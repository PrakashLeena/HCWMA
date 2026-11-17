"""Step 9: the approval service — one prompt, one exact action, one use, short-lived."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest

from integrations.mcp.tool_schemas import PermissionTier
from security.approvals.approval_service import ApprovalAnswer, ApprovalService, action_hash
from security.audit.audit_logger import AuditLogger

ARGS = {"path": "C:/docs/a.txt"}
WHO = {"task_id": "t1", "agent": "file_system", "tool": "delete_file"}


def _service(tmp_path: Path, timeout_s: float = 5) -> tuple[ApprovalService, AuditLogger]:
    audit = AuditLogger(tmp_path / "audit.jsonl")
    return ApprovalService(audit, timeout_s=timeout_s), audit


async def _ask(service: ApprovalService, **over: Any) -> ApprovalAnswer:
    fields = {**WHO, "tier": PermissionTier.T4, "arguments": ARGS, "summary": "Delete a.txt"}
    return await service.request(**{**fields, **over})


def _outcomes(audit: AuditLogger) -> list[str]:
    return [r.outcome for r in audit.read_all() if r.event == "approval"]


def test_prompt_is_shown_and_an_approval_works_exactly_once(tmp_path: Path) -> None:
    service, audit = _service(tmp_path)

    async def go() -> None:
        async with anyio.create_task_group() as tg:
            answer: list[ApprovalAnswer] = []

            async def ask() -> None:
                answer.append(await _ask(service))

            tg.start_soon(ask)
            while not service.pending():
                await anyio.sleep(0.01)
            (prompt,) = service.pending()
            assert (prompt.tool, prompt.agent, prompt.tier) == ("delete_file", "file_system", "T4")
            assert prompt.summary == "Delete a.txt" and prompt.arguments == ARGS
            assert 0 < prompt.expires_in_s <= 5
            assert service.decide(prompt.id, True)
            assert not service.decide(prompt.id, False)  # an answer cannot be changed
        (got,) = answer
        assert got.outcome == "approved" and service.pending() == []
        assert service.consume(got.approval_id, **WHO, arguments=ARGS)
        assert not service.consume(got.approval_id, **WHO, arguments=ARGS)  # single use

    anyio.run(go)
    assert _outcomes(audit) == ["requested", "approved", "used"]


@pytest.mark.parametrize(
    "change",
    [
        {"arguments": {"path": "C:/docs/OTHER.txt"}},
        {"tool": "move_file"},
        {"agent": "coding"},
        {"task_id": "t2"},
    ],
)
def test_an_approval_is_bound_to_the_exact_action(tmp_path: Path, change: dict[str, Any]) -> None:
    service, audit = _service(tmp_path)

    async def go() -> bool:
        async with anyio.create_task_group() as tg:
            answer: list[ApprovalAnswer] = []

            async def ask() -> None:
                answer.append(await _ask(service))

            tg.start_soon(ask)
            while not service.pending():
                await anyio.sleep(0.01)
            service.decide(service.pending()[0].id, True)
        used_as = {**WHO, "arguments": ARGS, **change}
        wrong = service.consume(answer[0].approval_id, **used_as)
        # a failed attempt burns the approval: it cannot be tried again with the right action
        return wrong or service.consume(answer[0].approval_id, **WHO, arguments=ARGS)

    assert anyio.run(go) is False
    assert _outcomes(audit)[-1] == "mismatch"


def test_deny_and_timeout_and_unknown_ids(tmp_path: Path) -> None:
    service, audit = _service(tmp_path, timeout_s=0.15)

    async def go() -> tuple[ApprovalAnswer, ApprovalAnswer]:
        answers: list[ApprovalAnswer] = []
        async with anyio.create_task_group() as tg:

            async def ask() -> None:
                answers.append(await _ask(service))

            tg.start_soon(ask)
            while not service.pending():
                await anyio.sleep(0.01)
            assert service.decide(service.pending()[0].id, False)
        ignored = await _ask(service)  # nobody answers
        assert not service.decide(ignored.approval_id, True)  # too late
        return answers[0], ignored

    denied, ignored = anyio.run(go)
    assert denied.outcome == "denied" and ignored.outcome == "expired"
    assert ignored.waited_s >= 0.15
    for answer in (denied, ignored):
        assert not service.consume(answer.approval_id, **WHO, arguments=ARGS)
    assert not service.decide("no-such-id", True)
    assert not service.consume("no-such-id", **WHO, arguments=ARGS)
    assert _outcomes(audit) == ["requested", "denied", "requested", "expired"]


def test_cancel_all_withdraws_every_prompt(tmp_path: Path) -> None:
    service, audit = _service(tmp_path)

    async def go() -> list[ApprovalAnswer]:
        answers: list[ApprovalAnswer] = []
        async with anyio.create_task_group() as tg:
            for task in ("t1", "t2"):

                async def ask(task: str = task) -> None:
                    answers.append(await _ask(service, task_id=task))

                tg.start_soon(ask)
            while len(service.pending()) < 2:
                await anyio.sleep(0.01)
            ids = [p.id for p in service.pending()]
            assert service.cancel_all() == 2
            assert service.pending() == []
            assert not any(service.decide(i, True) for i in ids)
        return answers

    answers = anyio.run(go)
    assert [a.outcome for a in answers] == ["cancelled", "cancelled"]
    assert _outcomes(audit).count("cancelled") == 2


def test_hash_ignores_key_order_but_not_values() -> None:
    assert action_hash("t", {"a": 1, "b": 2}) == action_hash("t", {"b": 2, "a": 1})
    assert action_hash("t", {"a": 1}) != action_hash("t", {"a": 2})
    assert action_hash("t", {"a": 1}) != action_hash("u", {"a": 1})
