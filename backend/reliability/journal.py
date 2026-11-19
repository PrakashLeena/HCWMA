"""Task journal: crash and sleep recovery (build step 18).

Every owner request is written here when it starts and again when it ends; every approval
prompt when it opens and when it closes. The journal is a small SQLite file in the HCWMA
data folder (`<data dir>/tasks/journal.sqlite3`).

On the next start (`recover()`), anything still marked running belongs to an app that
crashed, was killed or lost power: each such task is marked **interrupted** and each such
approval **cancelled**, an audit record is written for each, and the owner is told. Nothing
is ever re-run: the journal holds no way to re-run a task (no tool calls, no arguments), only
a short label so the owner knows which request it was.

When the PC sleeps the app keeps its process, so the backend's sleep watch calls
`interrupt()` for what was running instead (`sleep_watch.py`).
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from memory.config import resolve_data_dir
from security.audit.audit_logger import AuditLogger

JOURNAL_DIR = "tasks"
JOURNAL_FILE = "journal.sqlite3"
LABEL_CHARS = 80
KEEP_ROWS = 500

TaskStatus = Literal["running", "done", "failed", "stopped", "interrupted"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    pid INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    tool TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    outcome TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def journal_path(data_dir: Path | None = None) -> Path:
    return (data_dir or resolve_data_dir()) / JOURNAL_DIR / JOURNAL_FILE


def short_label(text: str) -> str:
    """One line, at most LABEL_CHARS characters: enough for the owner to recognise it."""
    flat = " ".join(text.split())
    return flat if len(flat) <= LABEL_CHARS else flat[: LABEL_CHARS - 1] + "…"


class JournalTask(BaseModel):
    id: str
    label: str
    started_at: str
    ended_at: str | None
    status: str
    reason: str


class RecoveryReport(BaseModel):
    """What the previous run left unfinished. Shown to the owner once, never re-run."""

    interrupted_tasks: list[JournalTask] = []
    cancelled_approvals: int = 0
    checked_at: str = ""

    def message(self) -> str:
        if not self.interrupted_tasks and not self.cancelled_approvals:
            return ""
        parts = []
        if self.interrupted_tasks:
            names = "; ".join(f'"{t.label}"' for t in self.interrupted_tasks[:3])
            parts.append(
                f"{len(self.interrupted_tasks)} request(s) were interrupted when HCWMA last "
                f"closed ({names}). They were NOT run again; ask again if you still want them."
            )
        if self.cancelled_approvals:
            parts.append(f"{self.cancelled_approvals} approval prompt(s) were cancelled.")
        return " ".join(parts)


class TaskJournal:
    def __init__(self, path: Path | None = None, audit: AuditLogger | None = None) -> None:
        self.path = path or journal_path()
        self._audit = audit
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(_SCHEMA)

    def _connect(self) -> closing[sqlite3.Connection]:
        # A fresh short-lived connection per call: safe from any thread, and every write is
        # committed before the call returns (a crash right after loses nothing).
        return closing(sqlite3.connect(self.path, timeout=5))

    def _run(self, sql: str, params: tuple[object, ...] = ()) -> int:
        with self._lock, self._connect() as db:
            cur = db.execute(sql, params)
            db.commit()
            return cur.rowcount

    def _rows(self, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
        with self._lock, self._connect() as db:
            return list(db.execute(sql, params).fetchall())

    # ---------- tasks ----------

    def start_task(self, task_id: str, label: str) -> None:
        self._run(
            "INSERT OR REPLACE INTO tasks (id, label, started_at, status, pid) "
            "VALUES (?, ?, ?, 'running', ?)",
            (task_id, short_label(label), _now(), os.getpid()),
        )

    def end_task(self, task_id: str, status: TaskStatus, reason: str = "") -> None:
        """Only a running task changes: an interrupted one stays interrupted."""
        self._run(
            "UPDATE tasks SET status = ?, reason = ?, ended_at = ? "
            "WHERE id = ? AND status = 'running'",
            (status, reason[:200], _now(), task_id),
        )

    def task(self, task_id: str) -> JournalTask | None:
        rows = self._rows(
            "SELECT id, label, started_at, ended_at, status, reason FROM tasks WHERE id = ?",
            (task_id,),
        )
        return _task(rows[0]) if rows else None

    def running(self) -> list[JournalTask]:
        return [
            _task(r)
            for r in self._rows(
                "SELECT id, label, started_at, ended_at, status, reason FROM tasks "
                "WHERE status = 'running' ORDER BY started_at"
            )
        ]

    def recent(self, limit: int = 10) -> list[JournalTask]:
        return [
            _task(r)
            for r in self._rows(
                "SELECT id, label, started_at, ended_at, status, reason FROM tasks "
                "ORDER BY started_at DESC, rowid DESC LIMIT ?",
                (limit,),
            )
        ]

    # ---------- approvals ----------

    def open_approval(self, approval_id: str, *, task_id: str, agent: str, tool: str) -> None:
        self._run(
            "INSERT OR REPLACE INTO approvals (id, task_id, agent, tool, opened_at, outcome) "
            "VALUES (?, ?, ?, ?, ?, 'pending')",
            (approval_id, task_id, agent, tool, _now()),
        )

    def close_approval(self, approval_id: str, outcome: str) -> None:
        self._run(
            "UPDATE approvals SET outcome = ?, closed_at = ? WHERE id = ? AND outcome = 'pending'",
            (outcome, _now(), approval_id),
        )

    def pending_approvals(self) -> list[tuple[str, str, str, str]]:
        """(id, task_id, agent, tool) of every prompt still marked pending."""
        return [
            (str(a), str(b), str(c), str(d))
            for a, b, c, d in self._rows(
                "SELECT id, task_id, agent, tool FROM approvals WHERE outcome = 'pending'"
            )
        ]

    # ---------- recovery ----------

    def interrupt(self, task_ids: list[str], reason: str) -> list[JournalTask]:
        """Mark these running tasks interrupted (the PC slept). Their approvals are cancelled
        by the stop control; nothing is re-run."""
        done = []
        for task_id in task_ids:
            if self._run(
                "UPDATE tasks SET status = 'interrupted', reason = ?, ended_at = ? "
                "WHERE id = ? AND status = 'running'",
                (reason[:200], _now(), task_id),
            ):
                task = self.task(task_id)
                if task is not None:
                    done.append(task)
                    self._audit_task(task)
        return done

    def recover(self) -> RecoveryReport:
        """Called once at start: whatever the previous run left running is interrupted, its
        approval prompts are cancelled. Returns what was found; never re-runs anything."""
        reason = "HCWMA closed while this was running (crash, forced quit, power loss)"
        tasks = self.running()
        for task in tasks:
            self._run(
                "UPDATE tasks SET status = 'interrupted', reason = ?, ended_at = ? "
                "WHERE id = ? AND status = 'running'",
                (reason, _now(), task.id),
            )
            self._audit_task(task.model_copy(update={"reason": reason}))
        approvals = self.pending_approvals()
        for approval_id, task_id, agent, tool in approvals:
            self.close_approval(approval_id, "cancelled")
            if self._audit is not None:
                self._audit.approval(
                    task_id=task_id,
                    agent=agent,
                    tool=tool,
                    args={},
                    outcome="cancelled",
                    reason=f"approval {approval_id[:8]} cancelled at start: HCWMA had closed",
                )
        self._prune()
        return RecoveryReport(
            interrupted_tasks=[t.model_copy(update={"status": "interrupted"}) for t in tasks],
            cancelled_approvals=len(approvals),
            checked_at=_now(),
        )

    def _audit_task(self, task: JournalTask) -> None:
        if self._audit is not None:
            self._audit.recovery(task_id=task.id, outcome="interrupted", reason=task.reason)

    def _prune(self) -> None:
        for table, col in (("tasks", "started_at"), ("approvals", "opened_at")):
            self._run(
                f"DELETE FROM {table} WHERE rowid NOT IN "  # noqa: S608 - fixed names above
                f"(SELECT rowid FROM {table} ORDER BY {col} DESC LIMIT ?)",
                (KEEP_ROWS,),
            )


def _task(row: tuple[object, ...]) -> JournalTask:
    task_id, label, started, ended, status, reason = row
    return JournalTask(
        id=str(task_id),
        label=str(label),
        started_at=str(started),
        ended_at=str(ended) if ended is not None else None,
        status=str(status),
        reason=str(reason),
    )
