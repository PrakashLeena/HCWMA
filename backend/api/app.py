"""The local HCWMA backend (master document 10.3 and 11, build step 5).

- Reached on 127.0.0.1 only; requests with any other Host header are refused.
- `/chat` needs the per-install token as `Authorization: Bearer <token>`.
- Inference goes through the shared model queue (architecture rule 3).
- No tools are offered to the model here. The Router and agents arrive in step 7.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from agents.runtime.model_queue import ModelQueue
from backend.auth.token import token_matches
from backend.config import BackendConfig
from backend.schemas.chat import ChatRequest, ChatResponse, HealthResponse
from model.serving import Message

logger = logging.getLogger("hcwma.backend")

WEB_DIR = Path(__file__).resolve().parents[2] / "app" / "web"

SYSTEM_PROMPT = (
    "You are HCWMA, a private assistant running on the owner's own PC. "
    "Answer clearly and briefly. You cannot open files, run programs or use the "
    "internet yet; say so if asked to."
)

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def create_app(queue: ModelQueue, token: str, config: BackendConfig) -> FastAPI:
    if not token:
        raise ValueError("the backend needs a non-empty API token")

    app = FastAPI(title="HCWMA", docs_url=None, redoc_url=None, openapi_url=None)
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
    def chat(request: ChatRequest) -> ChatResponse:
        messages = [Message(role="system", content=SYSTEM_PROMPT)]
        messages += [Message(role=t.role, content=t.content) for t in request.messages]
        try:
            result = queue.generate(messages, tools=None, max_tokens=config.chat_max_tokens)
        except Exception as exc:
            # Log the error type only: no message content and no runtime internals.
            logger.error("model call failed: %s", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="the local model is not available; check that Ollama is running",
            ) from None
        return ChatResponse(
            reply=result.text.strip(),
            completion_tokens=result.completion_tokens,
            tokens_per_s=round(result.tokens_per_s, 2),
            total_s=round(result.total_s, 2),
        )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", media_type="text/html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app
