"""The local HCWMA backend (master document 10.3 and 11, build step 5).

- Reached on 127.0.0.1 only; requests with any other Host header are refused.
- `/chat` needs the per-install token as `Authorization: Bearer <token>`.
- Every chat request goes to the Router (step 7), which delegates to sub-agents. The
  Router and its tool gateway (local MCP server) live as long as the app (lifespan).
- Inference goes through the shared model queue (architecture rule 3).
- `/approvals` and `/stop` (step 9) are how the owner answers an approval prompt and halts
  every agent. Like `/chat`, they need the token.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Protocol

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from agents.router.router import RouterReply
from agents.runtime.model_lock import BUSY_MESSAGE, ModelBusy, check_free
from backend.auth.token import token_matches
from backend.config import BackendConfig
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

logger = logging.getLogger("hcwma.backend")

WEB_DIR = Path(__file__).resolve().parents[2] / "app" / "web"


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
) -> FastAPI:
    if not token:
        raise ValueError("the backend needs a non-empty API token")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with handler_factory() as handler:
            app.state.handler = handler
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
        try:
            check_free()  # a sandbox tool test holds the model (build step 13)
            reply = await http.app.state.handler.handle(conversation)
        except ModelBusy:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=BUSY_MESSAGE
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

    @app.get("/tools", include_in_schema=False)
    def tools_page() -> FileResponse:
        return FileResponse(WEB_DIR / "tools.html", media_type="text/html")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", media_type="text/html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app
