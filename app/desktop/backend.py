"""The shell's link to the local backend: run it in a background thread, and a small client
for the three things the tray needs (is it up, what waits for approval, stop everything).

Everything here talks to 127.0.0.1 only. Nothing in this module needs Qt.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any

import uvicorn
from fastapi import FastAPI

from backend.config import BackendConfig


class BackendClient:
    def __init__(self, config: BackendConfig, token: str, timeout_s: float = 3.0) -> None:
        # BackendConfig.host can only be 127.0.0.1, so this client cannot reach elsewhere.
        self.base_url = f"http://{config.host}:{config.port}"
        self._token = token
        self._timeout_s = timeout_s
        # Never through a system proxy: the token must not leave this PC.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(self, method: str, path: str, auth: bool = True) -> Any:
        request = urllib.request.Request(  # noqa: S310 - fixed http://127.0.0.1 base URL
            self.base_url + path, method=method, data=b"" if method == "POST" else None
        )
        if auth:
            request.add_header("Authorization", f"Bearer {self._token}")
        with self._opener.open(request, timeout=self._timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))

    def is_up(self) -> bool:
        try:
            return self._request("GET", "/health", auth=False).get("status") == "ok"
        except (OSError, ValueError):
            return False

    def token_accepted(self) -> bool:
        """False if a backend is listening but was started with another token."""
        try:
            self._request("GET", "/approvals")
        except urllib.error.HTTPError:
            return False
        except (OSError, ValueError):
            return False
        return True

    def pending_approvals(self) -> list[dict[str, Any]]:
        try:
            return list(self._request("GET", "/approvals")["pending"])
        except (OSError, ValueError, KeyError):
            return []

    def stop_everything(self) -> dict[str, int] | None:
        """The stop report, or None if the backend could not be reached."""
        try:
            return dict(self._request("POST", "/stop"))
        except (OSError, ValueError):
            return None


class BackendThread:
    """Runs the FastAPI backend with uvicorn in a daemon thread (one worker, no reload)."""

    def __init__(self, app: FastAPI, config: BackendConfig) -> None:
        self._server = uvicorn.Server(
            uvicorn.Config(app, host=config.host, port=config.port, log_level="warning")
        )
        self._thread = threading.Thread(target=self._server.run, name="hcwma-backend", daemon=True)

    def start(self, timeout_s: float = 60.0) -> bool:
        """True once the backend accepts requests (the first PC file scan runs before that)."""
        self._thread.start()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._server.started:
                return True
            if not self._thread.is_alive():
                return False
            time.sleep(0.05)
        return False

    def stop(self, timeout_s: float = 10.0) -> None:
        self._server.should_exit = True
        if self._thread.is_alive():
            self._thread.join(timeout=timeout_s)
