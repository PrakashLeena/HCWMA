"""Step 9: the desktop shell.

- `BackendThread` + `BackendClient` against a real uvicorn server on 127.0.0.1 (a free
  port), with a stand-in Router that waits on a real approval prompt.
- The tray icon and its menu, built for real on Qt's "offscreen" platform (no window is
  shown and the embedded browser is not started).
- The two rules that keep the token on this PC: the window may only open the backend's own
  pages, and the token script only runs on that origin.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.desktop.backend import BackendClient, BackendThread  # noqa: E402
from app.desktop.shell import (  # noqa: E402
    Tray,
    is_backend_url,
    make_icon,
    stop_message,
    token_script,
)
from backend.api.app import create_app  # noqa: E402
from backend.config import BackendConfig  # noqa: E402
from security.audit.audit_logger import AuditLogger  # noqa: E402
from tests.backend.api.test_approvals_api import CHAT, TOKEN, ApprovingRouter  # noqa: E402

BASE = "http://127.0.0.1:8765"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------- backend thread + client ----------


@pytest.fixture
def running(tmp_path: Path) -> Iterator[tuple[BackendClient, ApprovingRouter, BackendConfig]]:
    config = BackendConfig(port=_free_port())
    router = ApprovingRouter(AuditLogger(tmp_path / "audit.jsonl"))
    server = BackendThread(create_app(router.factory, TOKEN, config), config)
    assert server.start(timeout_s=20)
    try:
        yield BackendClient(config, TOKEN), router, config
    finally:
        server.stop()


def test_backend_thread_serves_on_localhost_and_stops(tmp_path: Path) -> None:
    config = BackendConfig(port=_free_port())
    router = ApprovingRouter(AuditLogger(tmp_path / "audit.jsonl"))
    client = BackendClient(config, TOKEN)
    assert client.base_url == f"http://127.0.0.1:{config.port}"
    assert not client.is_up() and client.stop_everything() is None  # nothing there yet

    server = BackendThread(create_app(router.factory, TOKEN, config), config)
    assert server.start(timeout_s=20)
    assert client.is_up() and client.token_accepted()
    assert not BackendClient(config, "another-token").token_accepted()
    server.stop()
    assert not client.is_up()


def test_tray_client_sees_the_prompt_and_stop_halts_the_chat(
    running: tuple[BackendClient, ApprovingRouter, BackendConfig],
) -> None:
    client, router, config = running
    assert client.pending_approvals() == []
    box: dict[str, Any] = {}

    def chat() -> None:
        box["r"] = httpx.post(
            f"{client.base_url}/chat",
            json=CHAT,
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=20,
        )

    thread = threading.Thread(target=chat, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not client.pending_approvals() and time.monotonic() < deadline:
        time.sleep(0.02)
    (prompt,) = client.pending_approvals()
    assert prompt["tool"] == "restore_file"

    assert client.stop_everything() == {"stopped_tasks": 1, "cancelled_approvals": 1}
    thread.join(timeout=10)
    assert box["r"].json()["stopped_by"] == "owner_stop" and router.ran == []
    assert client.pending_approvals() == []
    assert BackendClient(config, "wrong").pending_approvals() == []  # no token, no data


# ---------- the token stays on this PC ----------


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        (BASE + "/", True),
        (BASE + "/static/chat.js", True),
        ("http://127.0.0.1:9999/", False),  # another local program
        ("http://localhost:8765/", False),
        ("https://127.0.0.1:8765/", False),
        ("http://127.0.0.1.evil.example:8765/", False),
        ("http://evil.example/", False),
        ("http://user@127.0.0.1:8765/", False),
        ("file:///C:/Windows/win.ini", False),
        ("javascript:alert(1)", False),
    ],
)
def test_window_only_opens_the_backends_own_pages(url: str, ok: bool) -> None:
    assert is_backend_url(url, BASE) is ok


def test_token_script_is_bound_to_the_backend_origin_and_safely_quoted() -> None:
    script = token_script('tok"en</script>\\', BASE)
    assert f'window.location.origin !== "{BASE}"' in script
    assert '"tok\\"en</script>\\\\"' in script  # JSON-quoted: cannot break out of the string
    assert "sessionStorage" in script and "localStorage" not in script


def test_token_is_never_put_in_a_url_or_on_disk() -> None:
    source = (Path(__file__).resolve().parents[3] / "app" / "desktop" / "shell.py").read_text(
        encoding="utf-8"
    )
    assert "?token" not in source and "setPersistentStoragePath" not in source
    assert "QWebEngineProfile(self)" in source  # unnamed profile = off the record


# ---------- tray icon ----------


class FakeBackend:
    def __init__(self) -> None:
        self.pending: list[dict[str, Any]] = []
        self.report: dict[str, int] | None = {"stopped_tasks": 1, "cancelled_approvals": 0}
        self.stops = 0
        self.due: list[dict[str, Any]] = []

    def pending_approvals(self) -> list[dict[str, Any]]:
        return self.pending

    def due_reminders(self) -> list[dict[str, Any]]:
        due, self.due = self.due, []
        return due

    def stop_everything(self) -> dict[str, int] | None:
        self.stops += 1
        return self.report

    # build step 18
    show = False
    recovery = ""
    warnings: list[str] = []  # noqa: RUF012 - replaced per test
    paused = False
    plugged_only = False
    reachable = True
    backups = 0

    def show_requested(self) -> bool:
        wanted, self.show = self.show, False
        return wanted

    def recovery_message(self) -> str:
        return self.recovery

    def resource_warnings(self) -> list[str]:
        return list(self.warnings)

    def set_model_paused(self, paused: bool) -> bool | None:
        if not self.reachable:
            return None
        self.paused = paused
        return paused

    def settings(self) -> dict[str, Any]:
        return {"heavy_only_when_plugged_in": self.plugged_only}

    def set_plugged_in_only(self, on: bool) -> bool | None:
        if not self.reachable:
            return None
        self.plugged_only = on
        return on

    def backup_now(self) -> str | None:
        self.backups += 1
        return "full-20261119-120000" if self.reachable else None


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication(["hcwma-test"])  # type: ignore[return-value]


def test_tray_menu_has_open_stop_and_quit(qt_app: QApplication) -> None:
    backend, opened, quits = FakeBackend(), [], []
    tray = Tray(backend, make_icon(), lambda: opened.append(1), lambda: quits.append(1))
    labels = [a.text() for a in tray.menu.actions() if not a.isSeparator()]
    assert labels == [
        "Open HCWMA",
        "Health",
        "Stop everything",
        "Pause model",
        "Heavy tasks only when plugged in",
        "Start with Windows",
        "Back up now",
        "Quit HCWMA",
    ]
    assert tray.icon.contextMenu() is tray.menu and not tray.icon.icon().isNull()

    tray.stop_action.trigger()
    assert backend.stops == 1
    tray.open_action.trigger()
    tray.quit_action.trigger()
    assert (opened, quits) == ([1], [1])


def test_tray_stop_reports_what_happened(qt_app: QApplication) -> None:
    backend = FakeBackend()
    tray = Tray(backend, make_icon(), lambda: None, lambda: None)
    assert tray.stop_everything().startswith("Stopped")
    backend.report = {"stopped_tasks": 0, "cancelled_approvals": 0}
    assert tray.stop_everything() == "Nothing was running."
    backend.report = None
    assert "Could not reach" in tray.stop_everything()
    assert stop_message({"stopped_tasks": 0, "cancelled_approvals": 2}).startswith("Stopped")


def test_tray_brings_the_window_forward_once_per_new_prompt(qt_app: QApplication) -> None:
    backend, opened = FakeBackend(), []
    tray = Tray(backend, make_icon(), lambda: opened.append(1), lambda: None)
    assert tray.check_approvals() == [] and opened == []

    backend.pending = [{"id": "a1", "tool": "restore_file"}]
    assert tray.check_approvals() == ["a1"] and opened == [1]
    assert "1 approval" in tray.icon.toolTip()
    assert tray.check_approvals() == [] and opened == [1]  # same prompt: no second pop-up

    backend.pending = []
    tray.check_approvals()
    assert tray.icon.toolTip() == "HCWMA"


def test_tray_shows_a_notification_for_due_reminders(qt_app: QApplication) -> None:
    """Step 15: a planner reminder becomes a desktop notification at its time."""
    backend = FakeBackend()
    tray = Tray(backend, make_icon(), lambda: None, lambda: None)
    shown: list[tuple[str, str]] = []
    tray.icon.showMessage = lambda title, text, *_: shown.append((title, text))  # type: ignore[method-assign]
    assert tray.check_reminders() is None and shown == []

    backend.due = [
        {"kind": "reminder", "title": "pay the rent", "due_text": "Fri 20 Nov 2026, 09:00"},
        {"kind": "deadline", "title": "essay", "due_text": "Fri 20 Nov 2026, 17:00"},
    ]
    text = tray.check_reminders()
    assert text == (
        "Reminder: pay the rent (Fri 20 Nov 2026, 09:00)\nDeadline: essay (Fri 20 Nov 2026, 17:00)"
    )
    assert shown == [("HCWMA reminder", text)]
    assert tray.check_reminders() is None  # each one is shown once
