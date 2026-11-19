""" "Stop everything" control (build step 9; master document 9.1).

Every user request the Router handles runs inside `StopControl.track(...)`. `stop_all()`
cancels all of them at once: the agent loops end at their next await (a model call or tool
call in flight is abandoned, and no further tool call starts), and every waiting approval
prompt is withdrawn so it can no longer be answered.

`stop_all()` must be called on the event loop that runs the tasks (the backend's `/stop`
endpoint does; the tray icon goes through that endpoint).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import anyio
from pydantic import BaseModel

from security.approvals.approval_service import ApprovalService
from security.audit.audit_logger import AuditLogger

if TYPE_CHECKING:
    from backend.reliability.journal import TaskJournal

STOPPED_BY_OWNER = "owner_stop"
INTERRUPTED = "interrupted"


class StopReport(BaseModel):
    stopped_tasks: int
    cancelled_approvals: int


class StopControl:
    def __init__(
        self,
        audit: AuditLogger,
        approvals: ApprovalService | None = None,
        journal: TaskJournal | None = None,
    ) -> None:
        self._audit = audit
        self._approvals = approvals
        self.journal = journal  # build step 18: crash and sleep recovery
        self._running: dict[str, anyio.CancelScope] = {}
        self._interrupted: dict[str, str] = {}

    def interrupted_reason(self, task_id: str) -> str | None:
        """Why the task was interrupted (the PC slept), or None if it was not."""
        return self._interrupted.get(task_id)

    @property
    def running(self) -> list[str]:
        return list(self._running)

    @contextmanager
    def track(self, task_id: str, label: str = "") -> Iterator[anyio.CancelScope]:
        """Run one user request so that Stop can halt it. Check `scope.cancelled_caught`.

        With a journal, the request is recorded as running until it ends, so a crash leaves a
        trace the next start marks interrupted (never re-run)."""
        if self.journal is not None:
            self.journal.start_task(task_id, label)
        status = "failed"
        with anyio.CancelScope() as scope:
            self._running[task_id] = scope
            try:
                yield scope
                status = "done"
            finally:
                self._running.pop(task_id, None)
                if scope.cancel_called:
                    status = "stopped"
                if self.journal is not None:
                    # An interrupted task is already marked so; this does not change it.
                    self.journal.end_task(task_id, status)  # type: ignore[arg-type]

    def interrupt_all(self, reason: str) -> StopReport:
        """The PC slept (step 18): halt what was running, withdraw every approval prompt and
        mark those tasks interrupted. Nothing is re-run on wake."""
        task_ids = list(self._running)
        for task_id in task_ids:
            self._interrupted[task_id] = reason
        if self.journal is not None:
            self.journal.interrupt(task_ids, reason)
        for task_id in task_ids:
            scope = self._running.get(task_id)
            if scope is not None:
                scope.cancel()
        cancelled = self._approvals.cancel_all() if self._approvals else 0
        return StopReport(stopped_tasks=len(task_ids), cancelled_approvals=cancelled)

    def stop_all(self) -> StopReport:
        task_ids = list(self._running)
        for scope in list(self._running.values()):
            scope.cancel()
        cancelled = self._approvals.cancel_all() if self._approvals else 0
        self._audit.stop(task_ids=task_ids, approvals_cancelled=cancelled)
        return StopReport(stopped_tasks=len(task_ids), cancelled_approvals=cancelled)
