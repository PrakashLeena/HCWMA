"""`python -m agents.router.timing`: split one request's time from the audit log."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agents.router.timing import latest_root, main, render, root_of, split


def _rec(ts: str, event: str, task: str, agent: str, **kw: Any) -> dict[str, Any]:
    return {
        "ts": f"2026-11-18T18:{ts}+00:00",
        "event": event,
        "task_id": task,
        "agent": agent,
        **kw,
    }


# The real "find folder amma" request (2026-11-18), before model calls were audited.
OLD = [
    _rec(
        "26:25.071",
        "delegation",
        "c2",
        "router",
        parent_task_id="R",
        target_agent="file_system",
        outcome="delegated",
    ),
    _rec(
        "27:54.798",
        "tool_call",
        "c2",
        "file_system",
        tool="search_files",
        duration_ms=397.77,
        outcome="ok",
    ),
    _rec(
        "28:17.960",
        "delegation",
        "c2",
        "router",
        parent_task_id="R",
        target_agent="file_system",
        outcome="done",
    ),
    _rec("28:17.986", "memory", "R", "system", outcome="stored"),
]

# The same request shape with model calls audited.
NEW = [
    _rec("00:35.000", "model_call", "R", "router", duration_ms=35000, outcome="ok"),
    _rec(
        "00:35.010",
        "delegation",
        "c2",
        "router",
        parent_task_id="R",
        target_agent="file_system",
        outcome="delegated",
    ),
    _rec("01:05.000", "model_call", "c2", "file_system", duration_ms=29000, outcome="ok"),
    _rec(
        "01:05.500",
        "tool_call",
        "c2",
        "file_system",
        tool="search_files",
        duration_ms=500,
        outcome="ok",
    ),
    _rec("01:15.500", "model_call", "c2", "file_system", duration_ms=10000, outcome="ok"),
    _rec(
        "01:15.600",
        "delegation",
        "c2",
        "router",
        parent_task_id="R",
        target_agent="file_system",
        outcome="done",
    ),
    _rec("09:00.000", "model_call", "OTHER", "router", duration_ms=1000, outcome="ok"),
]


def test_old_request_splits_at_the_tool_call() -> None:
    s = split(OLD, "R")
    assert s.router_model_s is None and s.agent_model_estimated
    assert [round(c, 1) for c in s.agent_model_s["file_system"]] == [89.3, 23.2]
    assert round(s.tool_total_s, 1) == 0.4
    text = render(s, total_s=148)
    assert "Router (inferred)      35.1 s" in text and "total                 148.0 s" in text


def test_audited_request_uses_measured_model_calls() -> None:
    s = split(NEW, "R")
    assert (s.router_model_s, s.router_calls) == (35.0, 1)
    assert s.agent_model_s == {"file_system": [29.0, 10.0]} and not s.agent_model_estimated
    assert s.tool_s == {"search_files": [0.5]}
    assert round(s.wall_s or 0, 1) == 75.6  # from the Router call's start to the last record
    assert "OTHER" not in render(s)


def test_any_task_id_finds_its_request_and_latest_is_the_newest() -> None:
    assert root_of(NEW, "c2") == "R" and root_of(NEW, "R") == "R"
    assert latest_root(NEW) == "OTHER"
    assert latest_root(OLD) == "R"


def test_cli_prints_the_split(tmp_path: Path, capsys: Any) -> None:
    log = tmp_path / "audit.jsonl"
    log.write_text("".join(json.dumps(r) + "\n" for r in OLD), encoding="utf-8")
    assert main(["c2", "--total", "148", "--log", str(log)]) == 0
    out = capsys.readouterr().out
    assert "file_system model calls" in out and "89.3, 23.2" in out
    assert main(["--log", str(tmp_path / "missing.jsonl")]) == 1
