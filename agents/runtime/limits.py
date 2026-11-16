"""Task limits enforced in code (architecture rule 14, master document 4.2).

- Delegation depth: at most `max_delegation_depth` (3) levels below the Router.
- Tool calls: at most `max_tool_calls_per_task` (20) per user request, shared by every
  agent in the delegation tree, so a loop split across agents is still stopped.
"""

from __future__ import annotations

import threading

from security.permissions.config import Limits


class LimitExceeded(Exception):
    def __init__(self, limit: str, message: str) -> None:
        super().__init__(message)
        self.limit = limit


class TaskBudget:
    """One per user request (root task). Thread-safe."""

    def __init__(self, limits: Limits) -> None:
        self.max_depth = limits.max_delegation_depth
        self.max_tool_calls = limits.max_tool_calls_per_task
        self._tool_calls = 0
        self._lock = threading.Lock()

    @property
    def tool_calls(self) -> int:
        return self._tool_calls

    def check_depth(self, depth: int) -> None:
        if depth > self.max_depth:
            raise LimitExceeded(
                "max_delegation_depth",
                f"delegation depth {depth} exceeds the limit of {self.max_depth}",
            )

    def take_tool_call(self) -> None:
        """Reserve one tool call before it runs; raises once the budget is used up."""
        with self._lock:
            if self._tool_calls >= self.max_tool_calls:
                raise LimitExceeded(
                    "max_tool_calls_per_task",
                    f"tool-call budget of {self.max_tool_calls} for this task is used up",
                )
            self._tool_calls += 1
