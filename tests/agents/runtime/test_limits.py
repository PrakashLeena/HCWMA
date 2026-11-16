"""Step 7: task limits in code (rule 14) — depth 3, 20 tool calls, read from permissions.yaml."""

from __future__ import annotations

import threading

import pytest

from agents.runtime.limits import LimitExceeded, TaskBudget
from security.permissions.config import Limits, load_permissions_config


def test_defaults_come_from_permissions_yaml() -> None:
    limits = load_permissions_config().limits
    assert (limits.max_delegation_depth, limits.max_tool_calls_per_task) == (3, 20)


def test_tool_call_budget() -> None:
    budget = TaskBudget(Limits(max_delegation_depth=3, max_tool_calls_per_task=3))
    for _ in range(3):
        budget.take_tool_call()
    with pytest.raises(LimitExceeded) as exc:
        budget.take_tool_call()
    assert exc.value.limit == "max_tool_calls_per_task"
    assert budget.tool_calls == 3


def test_depth() -> None:
    budget = TaskBudget(Limits(max_delegation_depth=3, max_tool_calls_per_task=20))
    budget.check_depth(3)
    with pytest.raises(LimitExceeded) as exc:
        budget.check_depth(4)
    assert exc.value.limit == "max_delegation_depth"


def test_budget_is_thread_safe() -> None:
    budget = TaskBudget(Limits(max_delegation_depth=3, max_tool_calls_per_task=20))
    taken: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(10):
            try:
                budget.take_tool_call()
            except LimitExceeded:
                return
            with lock:
                taken.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(taken) == 20 == budget.tool_calls


def test_limits_cannot_be_configured_past_the_design_maximum() -> None:
    with pytest.raises(ValueError):
        Limits(max_delegation_depth=4, max_tool_calls_per_task=20)
