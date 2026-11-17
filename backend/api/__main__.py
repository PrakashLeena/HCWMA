"""Start the local backend:  python -m backend.api   (Ctrl+C to stop).

The desktop app (`python -m app.desktop`) starts the same backend itself.
"""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI

from agents.router.factory import open_router
from backend.api.app import create_app
from backend.auth.token import get_or_create_token
from backend.config import BackendConfig, load_backend_config


def build_app(config: BackendConfig, token: str) -> FastAPI:
    """The real backend: Router, tool gateway, approval service, PC file index + watcher."""
    return create_app(
        handler_factory=lambda: open_router(
            max_tokens=config.chat_max_tokens, keep_file_index_fresh=True
        ),
        token=token,
        config=config,
    )


def main() -> int:
    config = load_backend_config()
    app = build_app(config, get_or_create_token())
    print(f"HCWMA chat page:  http://{config.host}:{config.port}/")
    print("API token (paste into the page):  python -m backend.auth.token")
    # config.host can only be 127.0.0.1 (see BackendConfig). No reload, one worker.
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
