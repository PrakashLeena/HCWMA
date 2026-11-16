"""Start the local backend:  python -m backend.api   (Ctrl+C to stop)."""

from __future__ import annotations

import uvicorn

from agents.runtime.model_queue import get_model_queue
from backend.api.app import create_app
from backend.auth.token import get_or_create_token
from backend.config import load_backend_config


def main() -> int:
    config = load_backend_config()
    app = create_app(queue=get_model_queue(), token=get_or_create_token(), config=config)
    print(f"HCWMA chat page:  http://{config.host}:{config.port}/")
    print("API token (paste into the page):  python -m backend.auth.token")
    # config.host can only be 127.0.0.1 (see BackendConfig). No reload, one worker.
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
