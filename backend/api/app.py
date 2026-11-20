"""The local HCWMA backend (master document 10.3 and 11, build step 5).

- Reached on 127.0.0.1 only; requests with any other Host header are refused.
- `/chat` needs the per-install token as `Authorization: Bearer <token>`.
- Every chat request goes to the Router (step 7), which delegates to sub-agents. The
  Router and its tool gateway (local MCP server) live as long as the app (lifespan).
- Inference goes through the shared model queue (architecture rule 3).
- `/approvals` and `/stop` (step 9) are how the owner answers an approval prompt and halts
  every agent. Like `/chat`, they need the token.
- `/voice/*` (step 14): push-to-talk. The backend opens the microphone only between
  `/voice/start` and `/voice/stop`, keeps the audio in memory and returns the transcript; the
  page then sends it to `/chat` like typed text. No voice route can answer an approval.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from agents.router.router import RouterReply
from agents.runtime.model_lock import BUSY_MESSAGE, ModelBusy, check_free
from agents.runtime.model_queue import PAUSED_MESSAGE, ModelPaused
from backend.auth.token import token_matches
from backend.config import BackendConfig
from backend.reliability.health import HealthReport
from backend.reliability.resources import low_resource_warnings
from backend.reliability.service import ReliabilityService
from backend.schemas.approvals import (
    ApprovalDecision,
    DecisionResponse,
    PendingApprovals,
    StopReport,
    ToolApproveRequest,
    ToolRollbackRequest,
)
from backend.schemas.chat import ChatRequest, ChatResponse, HealthResponse
from integrations.adapters.local_data.planner_store import PlannerStore
from integrations.adapters.local_data.when_parser import describe
from integrations.generated_tools.review import ReviewRefused, ReviewService
from model.serving import Message
from model.serving.ollama_launcher import OllamaLauncher
from voice.memory_budget import top_apps_by_ram
from voice.service import Transcript, VoiceRefused, VoiceService, VoiceStatus

logger = logging.getLogger("hcwma.backend")

WEB_DIR = Path(__file__).resolve().parents[2] / "app" / "web"


class SettingsChange(BaseModel):
    heavy_only_when_plugged_in: bool


class VoiceStart(BaseModel):
    language: Literal["en", "ta", "si"] = "en"


class VoiceSpeak(BaseModel):
    text: str = Field(max_length=20_000)
    language: Literal["en", "ta", "si"] | None = None


class ChatHandler(Protocol):
    """The Router (agents.router.router.Router) or a test double."""

    async def handle(self, conversation: list[Message]) -> RouterReply: ...


HandlerFactory = Callable[[], AbstractAsyncContextManager[ChatHandler]]

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def create_app(
    handler_factory: HandlerFactory,
    token: str,
    config: BackendConfig,
    review: ReviewService | None = None,
    planner: PlannerStore | None = None,
    clock: Callable[[], datetime] | None = None,
    reliability: ReliabilityService | None = None,
    voice: VoiceService | None = None,
    ollama: OllamaLauncher | None = None,
) -> FastAPI:
    if not token:
        raise ValueError("the backend needs a non-empty API token")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if reliability is not None:
            # Step 18: before any new request, whatever a crash left running is marked
            # interrupted (never re-run) and its approval prompts are cancelled.
            reliability.recover()
        if ollama is not None:
            # Owner decision 2026-11-20: start Ollama at HCWMA launch if it is down. In the
            # background, so the window opens at once; a chat meanwhile waits on the same lock.
            threading.Thread(target=ollama.ensure_running, name="ollama-start", daemon=True).start()
        async with handler_factory() as handler:
            app.state.handler = handler
            if reliability is None:
                yield
            else:
                async with reliability.run(lambda: getattr(handler, "stop", None)):
                    yield

    app = FastAPI(title="HCWMA", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    # No CORS middleware on purpose: no other origin may call this API.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[config.host])

    @app.middleware("http")
    async def _security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers[name] = value
        return response

    bearer = HTTPBearer(auto_error=False)

    def require_token(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),  # noqa: B008
    ) -> None:
        presented = credentials.credentials if credentials else None
        if not token_matches(token, presented):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing or wrong API token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.get("/health")
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.post("/chat", dependencies=[Depends(require_token)])
    async def chat(
        request: ChatRequest, http: Request, background: BackgroundTasks
    ) -> ChatResponse:
        conversation = [Message(role=t.role, content=t.content) for t in request.messages]
        start = time.perf_counter()
        if reliability is not None:
            # Step 18: every chat request runs Qwen (a heavy task). Refused while the model is
            # paused, RAM or disk is low, or on battery when the owner asked for that.
            refused = reliability.refusal()
            if refused is not None:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=refused)
        if ollama is not None:
            # Ollama down: start it (once, under a lock) and wait up to 60 s, then go on.
            started = await run_in_threadpool(ollama.ensure_running)
            if not started.ok:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=started.message()
                )
        try:
            check_free()  # a sandbox tool test holds the model (build step 13)
            reply = await http.app.state.handler.handle(conversation)
        except ModelBusy:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=BUSY_MESSAGE
            ) from None
        except ModelPaused:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=PAUSED_MESSAGE
            ) from None
        except Exception as exc:
            # Log the error type only: no message content and no runtime internals.
            logger.error("chat request failed: %s", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="the local model is not available; check that Ollama is running",
            ) from None
        summarize = getattr(http.app.state.handler, "summarize", None)
        if summarize is not None and request.session_id:
            # After the reply is sent (step 10): one short summary per chat session.
            background.add_task(summarize, conversation, request.session_id)
        return ChatResponse(
            reply=reply.reply.strip(),
            action=reply.action,
            status=reply.status,
            agents=reply.agents,
            tool_calls=reply.tool_calls,
            stopped_by=reply.stopped_by,
            task_id=reply.task_id,
            total_s=round(time.perf_counter() - start, 2),
        )

    # --- build step 9: approval prompts and the stop control -------------------------------
    # These are the only way to answer a prompt or to stop. They need the per-install token,
    # so only the owner's app can use them; no agent or tool has a path to them. They are
    # `async def` on purpose: both services must be driven from the event loop.

    @app.get("/approvals", dependencies=[Depends(require_token)])
    async def approvals(http: Request) -> PendingApprovals:
        service = getattr(http.app.state.handler, "approvals", None)
        return PendingApprovals(pending=service.pending() if service else [])

    @app.post("/approvals/{approval_id}", dependencies=[Depends(require_token)])
    async def decide(approval_id: str, body: ApprovalDecision, http: Request) -> DecisionResponse:
        service = getattr(http.app.state.handler, "approvals", None)
        if service is None or not service.decide(approval_id, body.decision == "approve"):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="that approval is no longer waiting (answered, expired or stopped)",
            )
        return DecisionResponse(id=approval_id, decision=body.decision)

    @app.post("/stop", dependencies=[Depends(require_token)])
    async def stop(http: Request) -> StopReport:
        if voice is not None:
            voice.cancel()  # Stop everything also closes the microphone and stops speaking
        control = getattr(http.app.state.handler, "stop", None)
        if control is None:
            return StopReport(stopped_tasks=0, cancelled_approvals=0)
        return control.stop_all()

    # --- build step 13: the owner's review of tools written by the Tool Builder -----------------
    # Token-protected like /approvals. Only these routes can approve, disable or roll back a
    # generated tool; no agent tool can reach them.

    def reviewer() -> ReviewService:
        if getattr(app.state, "review", None) is None:
            app.state.review = review or ReviewService.default()
        service: ReviewService = app.state.review
        return service

    def refuse(exc: ReviewRefused) -> HTTPException:
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    @app.get("/generated-tools", dependencies=[Depends(require_token)])
    def generated_tools() -> dict[str, list[dict[str, object]]]:
        return {"tools": reviewer().list_tools()}

    @app.get("/generated-tools/{name}", dependencies=[Depends(require_token)])
    def generated_tool(name: str) -> dict[str, object]:
        try:
            return reviewer().review(name)
        except ReviewRefused as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None

    @app.post("/generated-tools/{name}/approve", dependencies=[Depends(require_token)])
    def approve_generated_tool(name: str, body: ToolApproveRequest) -> dict[str, object]:
        try:
            return reviewer().approve(name, body.hash, body.agents)
        except ReviewRefused as exc:
            raise refuse(exc) from None

    @app.post("/generated-tools/{name}/disable", dependencies=[Depends(require_token)])
    def disable_generated_tool(name: str) -> dict[str, object]:
        try:
            return reviewer().disable(name)
        except ReviewRefused as exc:
            raise refuse(exc) from None

    @app.post("/generated-tools/{name}/enable", dependencies=[Depends(require_token)])
    def enable_generated_tool(name: str) -> dict[str, object]:
        try:
            return reviewer().enable(name)
        except ReviewRefused as exc:
            raise refuse(exc) from None

    @app.post("/generated-tools/{name}/rollback", dependencies=[Depends(require_token)])
    def rollback_generated_tool(name: str, body: ToolRollbackRequest) -> dict[str, object]:
        try:
            return reviewer().rollback(name, body.hash)
        except ReviewRefused as exc:
            raise refuse(exc) from None

    # --- build step 15: reminders and deadlines that are due --------------------------------
    # The tray app asks every few seconds and shows a desktop notification for each one.
    # POST because it marks them shown, so each one is notified once.

    def planner_store() -> PlannerStore:
        if getattr(app.state, "planner", None) is None:
            app.state.planner = planner or PlannerStore.default()
        store: PlannerStore = app.state.planner
        return store

    @app.post("/reminders/due", dependencies=[Depends(require_token)])
    def reminders_due() -> dict[str, list[dict[str, object]]]:
        now = (clock or datetime.now)()
        due = planner_store().take_due(now)
        return {
            "due": [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "title": item.title,
                    "due": item.due,
                    "due_text": describe(datetime.fromisoformat(item.due)) if item.due else "",
                }
                for item in due
            ]
        }

    # --- build step 18: reliability (health page, pause model, settings, backup) -----------
    # Owner-only, like /approvals: they need the per-install token; no agent tool reaches them.

    def rel() -> ReliabilityService:
        if reliability is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not available")
        return reliability

    @app.get("/health/details", dependencies=[Depends(require_token)])
    def health_details() -> HealthReport:
        return rel().health()

    @app.get("/recovery", dependencies=[Depends(require_token)])
    def recovery() -> dict[str, object]:
        report = rel().recovery
        return {"message": report.message(), "tasks": [t.label for t in report.interrupted_tasks]}

    @app.get("/resources", dependencies=[Depends(require_token)])
    def resources() -> dict[str, object]:
        service = rel()
        return {
            "warnings": low_resource_warnings(service.snapshot(), service.current_settings()),
            "model_paused": service.model.paused,
        }

    @app.get("/model/state", dependencies=[Depends(require_token)])
    def model_state() -> dict[str, str]:
        """The chat page asks this while a request waits: "starting" = Ollama is being started."""
        return {"state": ollama.state if ollama is not None else "unknown"}

    @app.post("/model/pause", dependencies=[Depends(require_token)])
    def pause_model() -> dict[str, bool]:
        rel().pause_model()
        return {"paused": True}

    @app.post("/model/resume", dependencies=[Depends(require_token)])
    def resume_model() -> dict[str, bool]:
        rel().resume_model()
        return {"paused": False}

    @app.get("/settings", dependencies=[Depends(require_token)])
    def get_settings() -> dict[str, object]:
        return rel().current_settings().model_dump()

    @app.post("/settings", dependencies=[Depends(require_token)])
    def change_settings(body: SettingsChange) -> dict[str, object]:
        return rel().update_settings(**body.model_dump()).model_dump()

    @app.post("/backup", dependencies=[Depends(require_token)])
    def backup_now() -> dict[str, str]:
        return {"backup": rel().backup_now("owner").name}

    @app.post("/app/show", dependencies=[Depends(require_token)])
    def ask_to_show() -> dict[str, bool]:
        """A second start of the app asks this one to bring its window forward."""
        rel().show_requested = True
        return {"ok": True}

    @app.post("/app/show/take", dependencies=[Depends(require_token)])
    def take_show_request() -> dict[str, bool]:
        service = rel()
        wanted, service.show_requested = service.show_requested, False
        return {"show": wanted}

    # --- build step 14: push-to-talk voice --------------------------------------------------
    # Owner-only, like /chat. The microphone is open only between /voice/start and /voice/stop
    # (the page holds the talk button or hotkey) and never longer than max_record_seconds.
    # The transcript is returned to the page, which shows it and sends it to /chat like typed
    # text: same Router, same allowlists, same approval cards. None of these routes can answer
    # an approval, and the transcript is never logged.

    def vs() -> VoiceService:
        if voice is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="voice is not available"
            )
        return voice

    @app.get("/voice", dependencies=[Depends(require_token)])
    def voice_status() -> VoiceStatus:
        return vs().status()

    @app.post("/voice/start", dependencies=[Depends(require_token)])
    def voice_start(body: VoiceStart) -> VoiceStatus:
        service = vs()
        if reliability is not None:
            refused = reliability.refusal()  # the request will run Qwen: same rules as /chat
            if refused is not None:
                if "low on resources" in refused:  # name the apps the owner could close
                    apps = ", ".join(f"{name} {mb} MB" for name, mb in top_apps_by_ram())
                    refused += f" Close some of these to free RAM: {apps}."
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=refused)
        try:
            service.start(body.language)
        except VoiceRefused as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
        return service.status()

    @app.post("/voice/stop", dependencies=[Depends(require_token)])
    async def voice_stop() -> Transcript:
        try:
            return await run_in_threadpool(vs().stop)
        except RuntimeError as exc:
            logger.error("voice transcription failed: %s", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from None

    @app.post("/voice/cancel", dependencies=[Depends(require_token)])
    def voice_cancel() -> dict[str, bool]:
        vs().cancel()
        return {"ok": True}

    @app.post("/voice/speak", dependencies=[Depends(require_token)])
    def voice_speak(body: VoiceSpeak) -> dict[str, str]:
        return vs().speak(body.text, body.language)

    @app.post("/voice/quiet", dependencies=[Depends(require_token)])
    def voice_quiet() -> dict[str, bool]:
        vs().quiet()
        return {"ok": True}

    @app.get("/status", include_in_schema=False)
    def health_page() -> FileResponse:
        return FileResponse(WEB_DIR / "health.html", media_type="text/html")

    @app.get("/tools", include_in_schema=False)
    def tools_page() -> FileResponse:
        return FileResponse(WEB_DIR / "tools.html", media_type="text/html")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", media_type="text/html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app
