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
from pathlib import Path
from typing import Protocol

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from agents.router.router import RouterReply
from backend.auth.token import token_matches
from backend.config import BackendConfig
from backend.schemas.approvals import (
    ApprovalDecision,
    DecisionResponse,
    PendingApprovals,
    StopReport,
)
from backend.schemas.chat import ChatRequest, ChatResponse, HealthResponse
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


def create_app(handler_factory: HandlerFactory, token: str, config: BackendConfig) -> FastAPI:
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
    async def chat(request: ChatRequest, http: Request) -> ChatResponse:
        conversation = [Message(role=t.role, content=t.content) for t in request.messages]
        start = time.perf_counter()
        try:
            reply = await http.app.state.handler.handle(conversation)
        except Exception as exc:
            # Log the error type only: no message content and no runtime internals.
            logger.error("chat request failed: %s", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="the local model is not available; check that Ollama is running",
            ) from None
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

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", media_type="text/html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app
