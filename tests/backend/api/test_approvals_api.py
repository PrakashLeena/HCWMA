"""Step 9: the owner's side of an approval and of Stop, over the local API.

A chat request waits on a real `ApprovalService`; the test plays the desktop app: it polls
`/approvals`, answers, or presses Stop, from a second connection while `/chat` is open.
"""

from __future__ import annotations

import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agents.router.router import RouterReply
from agents.runtime.stop import StopControl
from backend.api.app import create_app
from backend.config import BackendConfig
from integrations.mcp.tool_schemas import PermissionTier
from model.serving import Message
from security.approvals.approval_service import ApprovalService
from security.audit.audit_logger import AuditLogger

TOKEN = "test-token-not-a-real-secret"  # noqa: S105
BASE_URL = "http://127.0.0.1:8765"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
CHAT = {"messages": [{"role": "user", "content": "restore my thesis"}]}


class ApprovingRouter:
    """Stands in for the Router: every chat asks to restore a file and reports the answer."""

    def __init__(self, audit: AuditLogger) -> None:
        self.approvals = ApprovalService(audit, timeout_s=10)
        self.stop = StopControl(audit, self.approvals)
        self.ran: list[str] = []

    async def handle(self, conversation: list[Message]) -> RouterReply:
        outcome = "stopped"
        with self.stop.track("task-1") as scope:
            answer = await self.approvals.request(
                task_id="task-1",
                agent="file_system",
                tool="restore_file",
                tier=PermissionTier.T3,
                arguments={"item_id": "C:|$RAB12CD.docx"},
                summary="Restore C:\\docs\\thesis.docx from the Recycle Bin",
            )
            outcome = answer.outcome
            if outcome == "approved":
                self.ran.append("restore_file")
        stopped = scope.cancelled_caught
        return RouterReply(
            task_id="task-1",
            action="delegate",
            reply=outcome,
            status="done" if outcome == "approved" else "failed",
            agents=["file_system"],
            stopped_by="owner_stop" if stopped else None,
        )

    @asynccontextmanager
    async def factory(self) -> AsyncIterator[ApprovingRouter]:
        yield self


@pytest.fixture
def router(tmp_path: Path) -> ApprovingRouter:
    return ApprovingRouter(AuditLogger(tmp_path / "audit.jsonl"))


@pytest.fixture
def client(router: ApprovingRouter) -> Iterator[TestClient]:
    app = create_app(handler_factory=router.factory, token=TOKEN, config=BackendConfig())
    with TestClient(app, base_url=BASE_URL) as c:
        yield c


def _chat_in_background(client: TestClient) -> tuple[threading.Thread, dict[str, Any]]:
    box: dict[str, Any] = {}

    def run() -> None:
        box["response"] = client.post("/chat", json=CHAT, headers=AUTH)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def _wait_for_prompt(client: TestClient) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        pending = client.get("/approvals", headers=AUTH).json()["pending"]
        if pending:
            return pending[0]
        time.sleep(0.02)
    raise AssertionError("no approval prompt appeared")


def _finish(thread: threading.Thread, box: dict[str, Any]) -> dict[str, Any]:
    thread.join(timeout=10)
    assert not thread.is_alive(), "/chat did not return"
    assert box["response"].status_code == 200
    return box["response"].json()


def test_the_prompt_says_what_will_happen_and_approve_lets_it_run(
    client: TestClient, router: ApprovingRouter
) -> None:
    thread, box = _chat_in_background(client)
    prompt = _wait_for_prompt(client)
    assert prompt["tool"] == "restore_file" and prompt["tier"] == "T3"
    assert prompt["agent"] == "file_system" and "thesis.docx" in prompt["summary"]
    assert router.ran == []  # paused: nothing has happened yet

    answer = client.post(f"/approvals/{prompt['id']}", json={"decision": "approve"}, headers=AUTH)
    assert answer.status_code == 200
    assert _finish(thread, box)["reply"] == "approved" and router.ran == ["restore_file"]
    assert client.get("/approvals", headers=AUTH).json()["pending"] == []
    # an answered prompt cannot be answered again
    again = client.post(f"/approvals/{prompt['id']}", json={"decision": "approve"}, headers=AUTH)
    assert again.status_code == 404


def test_deny_blocks_it(client: TestClient, router: ApprovingRouter) -> None:
    thread, box = _chat_in_background(client)
    prompt = _wait_for_prompt(client)
    client.post(f"/approvals/{prompt['id']}", json={"decision": "deny"}, headers=AUTH)
    body = _finish(thread, box)
    assert body["reply"] == "denied" and body["status"] == "failed" and router.ran == []


def test_stop_halts_the_running_chat_and_withdraws_the_prompt(
    client: TestClient, router: ApprovingRouter
) -> None:
    thread, box = _chat_in_background(client)
    prompt = _wait_for_prompt(client)
    stopped = client.post("/stop", headers=AUTH)
    assert stopped.status_code == 200
    assert stopped.json() == {"stopped_tasks": 1, "cancelled_approvals": 1}
    body = _finish(thread, box)
    assert body["stopped_by"] == "owner_stop" and router.ran == []
    late = client.post(f"/approvals/{prompt['id']}", json={"decision": "approve"}, headers=AUTH)
    assert late.status_code == 404
    assert client.post("/stop", headers=AUTH).json()["stopped_tasks"] == 0


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
def test_approvals_and_stop_need_the_token(
    client: TestClient, router: ApprovingRouter, headers: dict[str, str]
) -> None:
    thread, box = _chat_in_background(client)
    prompt = _wait_for_prompt(client)
    assert client.get("/approvals", headers=headers).status_code == 401
    assert client.post("/stop", headers=headers).status_code == 401
    answer = client.post(
        f"/approvals/{prompt['id']}", json={"decision": "approve"}, headers=headers
    )
    assert answer.status_code == 401
    assert client.post(f"/approvals/{prompt['id']}?token={TOKEN}", json={}).status_code == 401
    assert router.ran == [] and len(router.approvals.pending()) == 1  # still waiting, untouched
    client.post("/stop", headers=AUTH)
    _finish(thread, box)


def test_bad_decisions_and_unknown_ids_are_rejected(client: TestClient) -> None:
    assert (
        client.post("/approvals/nope", json={"decision": "approve"}, headers=AUTH).status_code
        == 404
    )
    for body in ({"decision": "always"}, {}, {"decision": "approve", "remember": True}):
        assert client.post("/approvals/nope", json=body, headers=AUTH).status_code == 422


def test_a_handler_without_the_services_still_answers(tmp_path: Path) -> None:
    from tests.backend.api.test_app import FakeRouter

    app = create_app(handler_factory=FakeRouter().factory, token=TOKEN, config=BackendConfig())
    with TestClient(app, base_url=BASE_URL) as c:
        assert c.get("/approvals", headers=AUTH).json() == {"pending": []}
        assert c.post("/stop", headers=AUTH).json()["stopped_tasks"] == 0
