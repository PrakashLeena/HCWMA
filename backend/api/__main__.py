"""Start the local backend:  python -m backend.api   (Ctrl+C to stop).

The desktop app (`python -m app.desktop`) starts the same backend itself. Only one HCWMA may
run at a time (step 18): this refuses to start while the desktop app or another backend runs.
"""

from __future__ import annotations

import sys

import uvicorn
from fastapi import FastAPI

from agents.router.factory import open_router
from backend.api.app import create_app
from backend.apps_service import AppsService, default_scanner
from backend.auth.token import get_or_create_token
from backend.config import BackendConfig, load_backend_config
from backend.reliability.instance_lock import InstanceLock
from backend.reliability.service import ReliabilityService
from model.serving.ollama_launcher import get_launcher
from voice.config import load_voice_config
from voice.service import VoiceService


def build_app(config: BackendConfig, token: str) -> FastAPI:
    """The real backend: Router, tool gateway, approval service, PC file index + watcher,
    memory service + its daily clean-up and backup job, and step 18's reliability service
    (task journal, crash/sleep recovery, daily full backup, health, resource guard), and
    step 14's push-to-talk voice service."""
    launcher = get_launcher()  # starts Ollama when it is down (owner decision 2026-11-20)
    reliability = ReliabilityService(ollama=launcher)
    return create_app(
        handler_factory=lambda: open_router(
            max_tokens=config.chat_max_tokens,
            keep_file_index_fresh=True,
            run_memory_maintenance=True,
            journal=reliability.journal,
        ),
        token=token,
        config=config,
        reliability=reliability,
        voice=VoiceService(load_voice_config()),
        ollama=launcher,
        apps=AppsService(default_scanner()),  # app inventory + adapters + setup (2026-11-21)
    )


def main() -> int:
    lock = InstanceLock()
    if not lock.acquire("backend"):
        print(lock.refusal(), file=sys.stderr)
        return 1
    try:
        config = load_backend_config()
        app = build_app(config, get_or_create_token())
        print(f"HCWMA chat page:  http://{config.host}:{config.port}/")
        print("API token (paste into the page):  python -m backend.auth.token")
        # config.host can only be 127.0.0.1 (see BackendConfig). No reload, one worker.
        uvicorn.run(app, host=config.host, port=config.port, log_level="info")
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
