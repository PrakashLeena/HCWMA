"""Verify check 10: every registered tool, called through the gateway, leaves exactly one
audit record. The audit write is central (`PolicyLayer.call_tool` -> `finish()`), so this
walks the real default catalogue and pins that no tool, and no outcome, escapes it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest

from agents.runtime.policy import PolicyLayer
from integrations.mcp.tool_schemas import CallerContext, PermissionTier, ToolResult
from integrations.registry.capability_catalog import default_catalog
from security.audit.audit_logger import AuditLogger
from security.permissions.permission_manager import (
    AgentGrant,
    PermissionManager,
    load_permissions_config,
)

# Minimal arguments that pass each tool's input model. A new tool must be added here, so a
# new tool can never skip this test.
VALID_ARGS: dict[str, dict[str, Any]] = {
    "apply_filter": {"paths": ["a.jpg"], "filter": "grayscale"},
    "apply_patch": {"path": "a.py", "edits": [{"old": "a", "new": "b"}]},
    "check_recycle_bin": {"query": "report"},
    "convert_format": {"paths": ["a.jpg"], "format": "png"},
    "crop_image": {"paths": ["a.jpg"]},
    "delete_file": {"path": "a.txt"},
    "get_disk_usage": {},
    "get_system_time": {},
    "image_info": {"path": "a.jpg"},
    "list_recent_files": {},
    "memory_delete": {"ids": ["0123456789ab"]},
    "memory_forget_all": {},
    "memory_search": {"query": "x"},
    "memory_write": {"text": "x", "scope": "profile"},
    "move_file": {"source": "a.txt", "destination": "b.txt"},
    "read_file": {"path": "a.py"},
    "read_logs": {},
    "reindex_folder": {},
    "resize_image": {"paths": ["a.jpg"], "width": 10},
    "restore_file": {"item_id": "X:|$R1"},
    "run_in_sandbox": {"command": "python a.py"},
    "run_tests": {},
    "search_files": {"query": "report"},
    "write_file": {"path": "a.py", "content": "x"},
}


class _EchoClient:
    """Stands in for the MCP client: the audit write is in the policy layer, not here."""

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout_s: float) -> ToolResult:
        return ToolResult.failure("tool_error", "stub")


def _gateway(tmp_path: Path) -> tuple[PolicyLayer, AuditLogger, AgentGrant, CallerContext]:
    catalog = default_catalog()
    audit = AuditLogger(tmp_path / "audit.jsonl")
    agent = AgentGrant(
        agent_id="all_tools", tools=frozenset(s.name for s in catalog.all_tools()), max_tier="T4"
    )
    policy = PolicyLayer(
        client=_EchoClient(),  # type: ignore[arg-type]
        catalog=catalog,
        permissions=PermissionManager(load_permissions_config()),
        audit=audit,
    )
    context = CallerContext(agent_id="all_tools", task_id="t", owner_words="hi")
    return policy, audit, agent, context


def test_every_registered_tool_has_valid_args_listed() -> None:
    names = {s.name for s in default_catalog().all_tools()}
    assert names == set(VALID_ARGS), "add new tools to VALID_ARGS"


@pytest.mark.parametrize("variant", ["empty_args", "valid_args"])
def test_one_audit_record_per_call_for_every_tool(tmp_path: Path, variant: str) -> None:
    policy, audit, agent, context = _gateway(tmp_path)
    tools = [s.name for s in default_catalog().all_tools()]

    async def go() -> list[tuple[str, ToolResult]]:
        out = []
        for name in tools:
            args = {} if variant == "empty_args" else dict(VALID_ARGS[name])
            out.append(
                (name, await policy.call_tool(f"task-{name}", agent, name, args, context=context))
            )
        return out

    results = anyio.run(go)
    records = audit.read_all()
    assert len(records) == len(tools) == len(results)
    for (name, result), record in zip(results, records, strict=True):
        assert record.tool == name
        assert record.task_id == f"task-{name}"
        assert record.agent == "all_tools"
        assert record.outcome == ("ok" if result.ok else (result.error_code or "error"))
    if variant == "valid_args":
        # T3/T4 tools stop at the approval gate (no approval service here) but are still audited.
        tiers = {s.name: s.manifest.permission_tier for s in default_catalog().all_tools()}
        gated = {r.tool for r in records if r.outcome == "needs_approval"}
        assert gated == {n for n, t in tiers.items() if t >= PermissionTier.T3}


def test_unknown_tool_and_refused_call_are_audited(tmp_path: Path) -> None:
    policy, audit, agent, _ = _gateway(tmp_path)
    nobody = AgentGrant(agent_id="router", tools=frozenset(), max_tier="T0")

    async def go() -> tuple[ToolResult, ToolResult]:
        return (
            await policy.call_tool("t1", agent, "no_such_tool", {}),
            await policy.call_tool("t2", nobody, "get_system_time", {}),
        )

    unknown, denied = anyio.run(go)
    assert (unknown.error_code, denied.error_code) == ("unknown_tool", "denied")
    assert [(r.tool, r.outcome) for r in audit.read_all()] == [
        ("no_such_tool", "unknown_tool"),
        ("get_system_time", "denied"),
    ]
