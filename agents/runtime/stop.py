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

import anyio
from pydantic import BaseModel

from security.approvals.approval_service import ApprovalService
from security.audit.audit_logger import AuditLogger

STOPPED_BY_OWNER = "owner_stop"


class StopReport(BaseModel):
    stopped_tasks: int
    cancelled_approvals: int


class StopControl:
    def __init__(self, audit: AuditLogger, approvals: ApprovalService | None = None) -> None:
        self._audit = audit
        self._approvals = approvals
        self._running: dict[str, anyio.CancelScope] = {}

    @property
    def running(self) -> list[str]:
        return list(self._running)

    @contextmanager
    def track(self, task_id: str) -> Iterator[anyio.CancelScope]:
        """Run one user request so that Stop can halt it. Check `scope.cancelled_caught`."""
        with anyio.CancelScope() as scope:
            self._running[task_id] = scope
            try:
                yield scope
            finally:
                self._running.pop(task_id, None)

    def stop_all(self) -> StopReport:
        task_ids = list(self._running)
        for scope in list(self._running.values()):
            scope.cancel()
        cancelled = self._approvals.cancel_all() if self._approvals else 0
        self._audit.stop(task_ids=task_ids, approvals_cancelled=cancelled)
        return StopReport(stopped_tasks=len(task_ids), cancelled_approvals=cancelled)
