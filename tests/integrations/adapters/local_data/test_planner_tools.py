"""Step 15: planner tools (add-tool skill test groups) over a temp database."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import anyio
import pytest

from agents.runtime.policy import PolicyLayer
from integrations.adapters.local_data.planner_store import PlannerStore
from integrations.adapters.local_data.planner_tools import (
    PLANNER_TOOLS_STATUS,
    PlannerDeps,
    planner_tool_specs,
)
from integrations.mcp.server import execute
from integrations.mcp.tool_schemas import ToolResult
from integrations.registry.capability_catalog import CapabilityCatalog, default_catalog
from security.audit.audit_logger import AuditLogger
from security.permissions.permission_manager import (
    AgentGrant,
    PermissionManager,
    load_permissions_config,
)

NOW = datetime(2026, 11, 19, 14, 30)  # Thursday


@pytest.fixture
def store(tmp_path: Path) -> PlannerStore:
    return PlannerStore(tmp_path / "planner" / "planner.sqlite3")


def _catalog(store: PlannerStore, status: str = "approved") -> CapabilityCatalog:
    catalog = CapabilityCatalog()
    for spec in planner_tool_specs(PlannerDeps(store, now=lambda: NOW), status=status):  # type: ignore[arg-type]
        catalog.register(spec)
    return catalog


def _run(catalog: CapabilityCatalog, tool: str, **args: Any) -> ToolResult:
    return anyio.run(execute, catalog, tool, args)


def _answer(catalog: CapabilityCatalog, tool: str, result: ToolResult) -> str | None:
    spec = catalog.get(tool)
    assert spec is not None and spec.answer is not None
    return spec.answer(spec.output_model.model_validate(result.data))


def test_add_works_out_the_date_in_code_and_answers_without_a_model(store: PlannerStore) -> None:
    catalog = _catalog(store)
    result = _run(catalog, "planner_add", title="pay the rent", when="on Friday")
    assert result.ok, result.error
    assert result.data is not None and result.data["item"]["due"] == "2026-11-20T09:00"
    text = _answer(catalog, "planner_add", result)
    assert text is not None
    assert text.startswith("Reminder set: pay the rent — Fri 20 Nov 2026, 09:00.")
    assert "desktop notification" in text
    assert store.items()[0].title == "pay the rent"


def test_a_todo_needs_no_date_but_a_reminder_does(store: PlannerStore) -> None:
    catalog = _catalog(store)
    todo = _run(catalog, "planner_add", title="buy milk", kind="todo")
    assert todo.ok and "no date" in (_answer(catalog, "planner_add", todo) or "")
    missing = _run(catalog, "planner_add", title="pay the rent")
    assert not missing.ok and "needs a day or time" in (missing.error or "")
    unclear = _run(catalog, "planner_add", title="x", when="whenever I feel like it")
    assert not unclear.ok and "ask the owner" in (unclear.error or "")
    past = _run(catalog, "planner_add", title="x", when="2026-11-01 10:00")
    assert not past.ok and "in the past" in (past.error or "")
    assert [i.title for i in store.items()] == ["buy milk"]


def test_invalid_arguments_are_rejected(store: PlannerStore) -> None:
    catalog = _catalog(store)
    for tool, args in (
        ("planner_add", {"title": "", "when": "Friday"}),
        ("planner_add", {"title": "x", "kind": "alarm"}),
        ("planner_add", {"title": "x", "surprise": 1}),
        ("planner_close", {"item_id": 0}),
        ("planner_list", {"kind": "meeting"}),
    ):
        result = _run(catalog, tool, **args)
        assert not result.ok and result.error_code == "invalid_arguments", (tool, args)


def test_list_by_day_and_close(store: PlannerStore) -> None:
    catalog = _catalog(store)
    _run(catalog, "planner_add", title="pay the rent", when="on Friday")
    _run(catalog, "planner_add", title="call the bank", when="tomorrow at 11")
    _run(catalog, "planner_add", title="dentist", when="on Monday")
    _run(catalog, "planner_add", title="buy milk", kind="todo")

    friday = _run(catalog, "planner_list", day="Friday")
    text = _answer(catalog, "planner_list", friday)
    assert text is not None and text.startswith("Planned (Fri 20 Nov 2026):")
    assert text.index("pay the rent") < text.index("call the bank")  # 09:00 before 11:00
    assert "dentist" not in text and "buy milk" not in text
    everything = _answer(catalog, "planner_list", _run(catalog, "planner_list"))
    assert everything is not None and everything.rstrip().endswith("buy milk (to-do)")

    closed = _run(catalog, "planner_close", item_id=1)
    assert _answer(catalog, "planner_close", closed) == "Marked done: #1 pay the rent."
    again = _run(catalog, "planner_close", item_id=1)
    assert not again.ok and "already done" in (again.error or "")
    nothing = _run(catalog, "planner_close", item_id=99)
    assert not nothing.ok and "no item #99" in (nothing.error or "")
    sunday = _answer(catalog, "planner_list", _run(catalog, "planner_list", day="Sunday"))
    assert sunday == "Nothing planned (Sun 22 Nov 2026)."
    assert store.get(1) is not None  # never erased, only marked


def test_due_reminders_are_taken_once(store: PlannerStore) -> None:
    store.add("reminder", "pay the rent", datetime(2026, 11, 20, 9, 0), NOW)
    store.add("deadline", "essay", datetime(2026, 11, 20, 17, 0), NOW)
    store.add("todo", "buy milk", None, NOW)
    store.add("reminder", "cancelled one", datetime(2026, 11, 20, 8, 0), NOW)
    store.close(4, "cancelled", NOW)
    assert store.take_due(datetime(2026, 11, 20, 8, 59)) == []
    due = store.take_due(datetime(2026, 11, 20, 9, 0))
    assert [i.title for i in due] == ["pay the rent"]
    assert store.take_due(datetime(2026, 11, 20, 9, 1)) == []  # shown once
    assert [i.title for i in store.take_due(datetime(2026, 11, 21))] == ["essay"]


def test_the_store_file_is_made_on_first_use_only(tmp_path: Path) -> None:
    path = tmp_path / "later" / "planner.sqlite3"
    store = PlannerStore(path)
    assert not path.exists()
    store.items()
    assert path.exists()


def test_new_tools_are_quarantined_until_the_owner_approves(store: PlannerStore) -> None:
    assert PLANNER_TOOLS_STATUS in ("quarantined", "approved")
    catalog = _catalog(store, status="quarantined")
    assert catalog.callable_tools() == []
    for name in ("planner_add", "planner_list", "planner_close"):
        spec = default_catalog().get(name)
        assert spec is not None and spec.manifest.route == "local_data"
        assert not spec.manifest.approval_required and not spec.manifest.permissions.network
        assert spec.manifest.side_effects in ("none", "writes")


def _policy(catalog: CapabilityCatalog, audit: AuditLogger) -> PolicyLayer:
    class Client:
        async def call_tool(self, name: str, arguments: dict[str, Any], timeout_s: float) -> Any:
            return await execute(catalog, name, arguments)

    return PolicyLayer(
        client=Client(),  # type: ignore[arg-type]
        catalog=catalog,
        permissions=PermissionManager(load_permissions_config()),
        audit=audit,
    )


def test_permission_denied_without_the_tool_and_every_call_audited(
    store: PlannerStore, tmp_path: Path
) -> None:
    catalog = _catalog(store)
    audit = AuditLogger(tmp_path / "audit.jsonl")
    policy = _policy(catalog, audit)
    planner = AgentGrant(agent_id="planner", tools=frozenset({"planner_add"}), max_tier="T2")
    other = AgentGrant(agent_id="echo", tools=frozenset(), max_tier="T0")

    async def go() -> tuple[ToolResult, ToolResult]:
        args = {"title": "x", "when": "Friday"}
        ok = await policy.call_tool("t1", planner, "planner_add", dict(args))
        denied = await policy.call_tool("t2", other, "planner_add", dict(args))
        return ok, denied

    ok, denied = anyio.run(go)
    assert ok.ok and not denied.ok and denied.error_code == "denied"
    records = [r for r in audit.read_all() if r.event == "tool_call"]
    assert [(r.agent, r.outcome) for r in records] == [("planner", "ok"), ("echo", "denied")]
    assert len(store.items()) == 1
