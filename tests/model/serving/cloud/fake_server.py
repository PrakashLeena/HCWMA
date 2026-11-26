"""A fake cloud provider on 127.0.0.1 (no real network in tests).

Each request is recorded (path, headers, JSON body); the reply is the next queued
(status, JSON) pair, or the default reply.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeProvider:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.replies: list[tuple[int, Any]] = []
        self.default: tuple[int, Any] = (500, {"error": "no reply queued"})
        self.url = ""

    def reply(self, body: Any, status: int = 200) -> None:
        self.replies.append((status, body))

    @property
    def last(self) -> dict[str, Any]:
        return self.requests[-1]


@contextmanager
def fake_provider() -> Iterator[FakeProvider]:
    state = FakeProvider()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server API
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            state.requests.append(
                {
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "raw": raw.decode("utf-8"),
                    "json": json.loads(raw or b"{}"),
                }
            )
            status, body = state.replies.pop(0) if state.replies else state.default
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def openai_reply(
    text: str = "", calls: list[tuple[str, Any]] | None = None, usage: tuple[int, int] = (10, 5)
) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": text or None}
    if calls:
        msg["tool_calls"] = [
            {
                "id": f"x{i}",
                "type": "function",
                "function": {
                    "name": n,
                    "arguments": a if isinstance(a, str) else json.dumps(a),
                },
            }
            for i, (n, a) in enumerate(calls)
        ]
    return {
        "choices": [{"message": msg}],
        "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]},
    }
