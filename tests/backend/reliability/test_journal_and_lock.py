"""Step 18: crash recovery and the single-instance lock, with real processes that are killed.

- A process starts a task and opens an approval prompt, then is killed (like a crash or a
  forced restart). The next start marks the task interrupted and the prompt cancelled,
  writes audit records, tells the owner — and runs nothing again.
- Only one HCWMA can hold the lock; a crashed holder never blocks the next start.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from backend.reliability.instance_lock import InstanceLock
from backend.reliability.journal import KEEP_ROWS, TaskJournal, short_label
from security.audit.audit_logger import AuditLogger

REPO = Path(__file__).resolve().parents[3]


def _child(code: str, *args: str) -> subprocess.Popen[str]:
    """A separate Python process running `code`; prints "ready" when it is set up."""
    return subprocess.Popen(  # noqa: S603 - the test's own interpreter and code
        [sys.executable, "-c", textwrap.dedent(code), *args],
        cwd=REPO,
        stdout=subprocess.PIPE,
        text=True,
    )


def _wait_ready(proc: subprocess.Popen[str]) -> None:
    assert proc.stdout is not None
    line = proc.stdout.readline().strip()
    assert line == "ready", line


def test_a_killed_process_leaves_an_interrupted_task_that_is_never_rerun(tmp_path: Path) -> None:
    db = tmp_path / "journal.sqlite3"
    proc = _child(
        """
        import sys, time
        from pathlib import Path
        from backend.reliability.journal import TaskJournal
        j = TaskJournal(Path(sys.argv[1]))
        j.start_task("t-done", "what time is it")
        j.end_task("t-done", "done")
        j.start_task("t-crash", "delete the old drafts in Documents")
        j.open_approval("a1", task_id="t-crash", agent="file_system", tool="delete_file")
        print("ready", flush=True)
        time.sleep(60)
        """,
        str(db),
    )
    _wait_ready(proc)
    proc.kill()  # the forced restart: no clean-up code runs
    proc.wait(timeout=10)

    audit = AuditLogger(tmp_path / "audit.jsonl")
    journal = TaskJournal(db, audit=audit)
    report = journal.recover()

    assert [t.id for t in report.interrupted_tasks] == ["t-crash"]
    assert report.cancelled_approvals == 1
    message = report.message()
    assert "delete the old drafts" in message and "NOT run again" in message
    assert journal.task("t-crash").status == "interrupted"  # type: ignore[union-attr]
    assert journal.task("t-done").status == "done"  # type: ignore[union-attr]
    assert journal.running() == [] and journal.pending_approvals() == []
    events = [(r.event, r.outcome, r.tool) for r in audit.read_all()]
    assert ("recovery", "interrupted", None) in events
    assert ("approval", "cancelled", "delete_file") in events
    # a second start finds nothing more: recovery happens once
    assert journal.recover().message() == ""


def test_an_interrupted_task_stays_interrupted(tmp_path: Path) -> None:
    journal = TaskJournal(tmp_path / "j.sqlite3")
    journal.start_task("t1", "x")
    journal.interrupt(["t1"], "the PC was asleep")
    journal.end_task("t1", "done")  # the cancelled request finishing later changes nothing
    assert journal.task("t1").status == "interrupted"  # type: ignore[union-attr]


def test_labels_are_short_and_old_rows_are_pruned(tmp_path: Path) -> None:
    assert short_label("a\n  b") == "a b"
    assert len(short_label("x" * 500)) == 80
    journal = TaskJournal(tmp_path / "j.sqlite3")
    for n in range(KEEP_ROWS + 20):
        journal.start_task(f"t{n}", "q")
        journal.end_task(f"t{n}", "done")
    journal.recover()
    assert len(journal.recent(10_000)) == KEEP_ROWS


# ---------- single instance ----------


def test_only_one_instance_holds_the_lock(tmp_path: Path) -> None:
    first, second = InstanceLock(tmp_path), InstanceLock(tmp_path)
    assert first.acquire("desktop app")
    assert not second.acquire("backend")
    assert "already running" in second.refusal() and "desktop app" in second.refusal()
    first.release()
    assert second.acquire("backend")
    second.release()


def test_another_process_holding_the_lock_blocks_a_second_start(tmp_path: Path) -> None:
    proc = _child(
        """
        import sys, time
        from pathlib import Path
        from backend.reliability.instance_lock import InstanceLock
        lock = InstanceLock(Path(sys.argv[1]))
        assert lock.acquire("desktop app")
        print("ready", flush=True)
        time.sleep(60)
        """,
        str(tmp_path),
    )
    try:
        _wait_ready(proc)
        mine = InstanceLock(tmp_path)
        assert not mine.acquire("desktop app")
        holder = mine.holder()
        assert holder is not None and holder["pid"] != os.getpid()
        assert f"process {holder['pid']}" in mine.refusal()
    finally:
        proc.kill()  # a crash: the OS frees the lock
        proc.wait(timeout=10)
    deadline = time.monotonic() + 5
    while not mine.acquire("desktop app"):
        assert time.monotonic() < deadline, "a crashed holder must not block the next start"
        time.sleep(0.1)
    mine.release()


@pytest.mark.parametrize("kind", ["desktop app", "backend"])
def test_both_entry_points_take_the_same_lock(kind: str, tmp_path: Path) -> None:
    held = InstanceLock(tmp_path)
    assert held.acquire(kind)
    assert not InstanceLock(tmp_path).acquire("desktop app" if kind == "backend" else "backend")
    held.release()
