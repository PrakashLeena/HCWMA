"""Step 18: a request in flight when the PC sleeps is interrupted, its approval prompt is
cancelled, the journal and audit log say so, and nothing is re-run or answered late."""

from __future__ import annotations

from pathlib import Path

import anyio

from agents.router.router import RouterReply
from agents.runtime.stop import INTERRUPTED
from backend.reliability.journal import TaskJournal
from model.serving import Message
from tests.agents.harness import stack
from tests.agents.runtime.test_approval_flow import _call, _events, _script, pc  # noqa: F401
from tests.integrations.adapters.pc.test_file_write_tools import WritePc


def test_pc_sleep_interrupts_the_task_and_cancels_its_prompt(pc: WritePc) -> None:  # noqa: F811
    target = pc.layout.docs / "notes" / "meeting notes.txt"
    model = _script(_call("delete_file", path=str(target)))
    journal = TaskJournal(pc.layout.tmp / "journal.sqlite3")

    async def go() -> tuple[RouterReply, bool, object]:
        async with stack(
            pc.layout.tmp / "run",
            model,
            catalog=pc.catalog(),
            approval_timeout_s=10,
            journal=journal,
        ) as s:
            assert s.approvals is not None
            late: list[bool] = []
            report: list[object] = []
            async with anyio.create_task_group() as tg:

                async def pc_sleeps_and_wakes() -> None:
                    while not s.approvals.pending():
                        await anyio.sleep(0.01)
                    # while asleep the task is still on the journal as running
                    assert [t.status for t in journal.running()] == ["running"]
                    prompt_id = s.approvals.pending()[0].id
                    report.append(s.stop.interrupt_all("the PC was asleep for about 40 minute(s)"))
                    late.append(s.approvals.decide(prompt_id, True))  # Approve after waking

                tg.start_soon(pc_sleeps_and_wakes)
                reply = await s.router.handle([Message(role="user", content="delete my notes")])
            return reply, late[0], s

    reply, late_approval_worked, s = anyio.run(go)
    assert reply.stopped_by == INTERRUPTED and reply.status == "failed"
    assert "asleep" in reply.reply and "not run again" in reply.reply
    assert late_approval_worked is False
    assert target.is_file() and pc.trashed == []  # the delete never ran
    assert ("delete_file", "cancelled") in _events(s, "approval")  # type: ignore[arg-type]
    [task] = journal.recent()
    assert task.status == "interrupted" and "asleep" in task.reason
    assert task.label == "delete my notes"
    assert journal.pending_approvals() == []  # the prompt was closed in the journal too


def test_finished_and_stopped_requests_are_journaled(tmp_path: Path) -> None:
    journal = TaskJournal(tmp_path / "journal.sqlite3")
    model = _script("Hello!")

    async def go() -> RouterReply:
        async with stack(tmp_path / "run", model, approval_timeout_s=5, journal=journal) as s:
            return await s.router.handle([Message(role="user", content="hi there")])

    anyio.run(go)
    [task] = journal.recent()
    assert task.status == "done" and task.label == "hi there" and journal.running() == []
