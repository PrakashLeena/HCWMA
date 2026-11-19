"""Where did the time go? Split one request into Router, agent model calls and tool time.

    python -m agents.router.timing                  # the latest request
    python -m agents.router.timing <task id>        # one request (Router task id or a
                                                    # delegated agent's task id)
    python -m agents.router.timing <id> --total 148 # also say what is left of a measured total

Reads the audit log only (it holds timings and argument summaries, never file contents).
Requests from before model calls were audited have no `model_call` records: then an agent's
model time is its delegation time minus its tool time, and the Router's time is only known
when `--total` is given.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from security.audit.audit_logger import DEFAULT_AUDIT_PATH


@dataclass
class TimingSplit:
    root_task_id: str
    start: datetime | None = None
    end: datetime | None = None
    router_model_s: float | None = None  # None: not audited (an older request)
    router_calls: int = 0
    agent_model_s: dict[str, list[float]] = field(default_factory=dict)
    agent_model_estimated: bool = False  # derived from delegation spans, not measured
    tool_s: dict[str, list[float]] = field(default_factory=dict)

    @property
    def wall_s(self) -> float | None:
        if self.start is None or self.end is None:
            return None
        return (self.end - self.start).total_seconds()

    @property
    def agent_total_s(self) -> float:
        return sum(sum(v) for v in self.agent_model_s.values())

    @property
    def tool_total_s(self) -> float:
        return sum(sum(v) for v in self.tool_s.values())


def _ts(record: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(record["ts"])


def _secs(record: dict[str, Any]) -> float:
    return float(record.get("duration_ms") or 0.0) / 1000


def load_records(path: Path = DEFAULT_AUDIT_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def latest_root(records: list[dict[str, Any]]) -> str | None:
    """The Router task id of the most recent request that delegated or called the model."""
    for record in reversed(records):
        if record.get("event") == "delegation" and record.get("parent_task_id"):
            return str(record["parent_task_id"])
        if record.get("event") == "model_call" and record.get("agent") == "router":
            return str(record["task_id"])
    return None


def root_of(records: list[dict[str, Any]], task_id: str) -> str:
    """Walk delegations up from `task_id` to the Router's task id."""
    parents = {
        r["task_id"]: r["parent_task_id"]
        for r in records
        if r.get("event") == "delegation" and r.get("parent_task_id")
    }
    seen = {task_id}
    while task_id in parents and parents[task_id] not in seen:
        task_id = parents[task_id]
        seen.add(task_id)
    return task_id


def split(records: Iterable[dict[str, Any]], root_task_id: str) -> TimingSplit:
    records = list(records)
    tasks = {root_task_id}
    changed = True
    while changed:  # delegated tasks, and tasks they delegated in turn (depth <= 3)
        changed = False
        for r in records:
            if r.get("event") == "delegation" and r.get("parent_task_id") in tasks:
                if r["task_id"] not in tasks:
                    tasks.add(r["task_id"])
                    changed = True
    mine = [r for r in records if r.get("task_id") in tasks]
    out = TimingSplit(root_task_id=root_task_id)
    if not mine:
        return out

    starts = [_ts(r) for r in mine]
    for r in mine:  # a record is written when its step ends: its start is ts - duration
        if r.get("duration_ms"):
            starts.append(datetime.fromtimestamp(_ts(r).timestamp() - _secs(r), _ts(r).tzinfo))
    out.start, out.end = min(starts), max(_ts(r) for r in mine)

    delegated_at: dict[str, datetime] = {}
    delegation_span: dict[str, tuple[str, float]] = {}
    audited_agents: set[str] = set()
    for r in mine:
        event = r.get("event")
        if event == "model_call":
            if r["agent"] == "router":
                out.router_model_s = (out.router_model_s or 0.0) + _secs(r)
                out.router_calls += 1
            else:
                out.agent_model_s.setdefault(r["agent"], []).append(_secs(r))
                audited_agents.add(r["task_id"])
        elif event == "tool_call":
            out.tool_s.setdefault(r.get("tool") or "?", []).append(_secs(r))
        elif event == "delegation" and r.get("parent_task_id") in tasks:
            if r.get("outcome") == "delegated":
                delegated_at[r["task_id"]] = _ts(r)
            elif r["task_id"] in delegated_at:
                span = (_ts(r) - delegated_at[r["task_id"]]).total_seconds()
                delegation_span[r["task_id"]] = (r["target_agent"], span)

    for task_id, (agent, span) in delegation_span.items():
        if task_id in audited_agents:
            continue
        # No model_call records: the gaps between tool calls are the model's turns.
        t0 = delegated_at[task_id].timestamp()
        cursor, calls = 0.0, []
        for r in sorted(
            (r for r in mine if r["task_id"] == task_id and r["event"] == "tool_call"), key=_ts
        ):
            end = _ts(r).timestamp() - t0
            calls.append(max(end - _secs(r) - cursor, 0.0))
            cursor = end
        calls.append(max(span - cursor, 0.0))
        out.agent_model_s.setdefault(agent, []).extend(calls)
        out.agent_model_estimated = True
    return out


def render(s: TimingSplit, total_s: float | None = None) -> str:
    wall = s.wall_s
    lines = [f"request {s.root_task_id}"]
    if s.start and s.end:
        lines.append(
            f"  audited span: {s.start:%Y-%m-%d %H:%M:%S} -> {s.end:%H:%M:%S} UTC  ({wall:.1f} s)"
        )
    total = total_s if total_s is not None else wall
    if s.router_model_s is not None:
        lines.append(f"  Router model        {s.router_model_s:7.1f} s  ({s.router_calls} call)")
    elif not s.agent_model_estimated and s.agent_model_s:
        # model calls are audited for this request, yet the Router made none
        lines.append("  Router                  0.0 s  (fast path: no model call)")
    elif total_s is not None and wall is not None:
        lines.append(
            f"  Router (inferred)   {total_s - wall:7.1f} s  "
            "(not audited: total minus audited span)"
        )
    else:
        lines.append("  Router              not audited for this request (pass --total)")
    for agent, calls in s.agent_model_s.items():
        how = "estimated: delegation minus tools" if s.agent_model_estimated else "measured"
        parts = ", ".join(f"{c:.1f}" for c in calls)
        lines.append(f"  {agent} model calls {sum(calls):5.1f} s  ({len(calls)}: {parts}; {how})")
    for tool, calls in s.tool_s.items():
        lines.append(f"  tool {tool:<14} {sum(calls):7.1f} s  ({len(calls)} call)")
    if total is not None:
        accounted = (s.router_model_s or 0.0) + s.agent_total_s + s.tool_total_s
        if s.router_model_s is None and total_s is not None and wall is not None:
            accounted += total_s - wall
        lines.append(f"  other (memory, overhead) {max(total - accounted, 0.0):5.1f} s")
        lines.append(f"  total               {total:7.1f} s")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("task_id", nargs="?", help="Router or agent task id (default: latest)")
    parser.add_argument("--total", type=float, help="the total you measured, in seconds")
    parser.add_argument("--log", type=Path, default=DEFAULT_AUDIT_PATH, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    records = load_records(args.log)
    root = root_of(records, args.task_id) if args.task_id else latest_root(records)
    if root is None:
        print("no request found in the audit log")
        return 1
    print(render(split(records, root), args.total))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
