"""Step 6 "Done when": the test tool is discovered and called through MCP, an audit record
is written, and a call from an agent without permission is refused."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
from mcp import Client
from pydantic import BaseModel

from agents.runtime.policy import PolicyLayer
from integrations.mcp.client import McpToolClient, local_server_params
from integrations.mcp.server import build_server
from integrations.mcp.tool_schemas import PermissionTier, ToolManifest, ToolSpec
from integrations.registry.capability_catalog import CapabilityCatalog, default_catalog
from security.audit.audit_logger import AuditLogger
from security.permissions.permission_manager import (
    AgentGrant,
    PermissionManager,
    load_permissions_config,
)

TIME_AGENT = AgentGrant(
    agent_id="time_tester", tools=frozenset({"get_system_time"}), max_tier=PermissionTier.T0
)
NO_TOOLS_AGENT = AgentGrant(agent_id="router", tools=frozenset(), max_tier=PermissionTier.T0)


def _policy(client: McpToolClient, audit: AuditLogger, catalog: CapabilityCatalog) -> PolicyLayer:
    return PolicyLayer(
        client=client,
        catalog=catalog,
        permissions=PermissionManager(load_permissions_config()),
        audit=audit,
    )


def test_done_when_over_real_stdio_mcp(tmp_path: Path) -> None:
    audit = AuditLogger(tmp_path / "audit.jsonl")

    async def go() -> tuple[list[str], list[str], Any, Any]:
        async with McpToolClient(Client(local_server_params())) as client:
            policy = _policy(client, audit, default_catalog())
            seen = [t.name for t in await policy.list_tools_for(TIME_AGENT)]
            hidden = [t.name for t in await policy.list_tools_for(NO_TOOLS_AGENT)]
            ok = await policy.call_tool("task-1", TIME_AGENT, "get_system_time", {})
            refused = await policy.call_tool("task-2", NO_TOOLS_AGENT, "get_system_time", {})
            return seen, hidden, ok, refused

    seen, hidden, ok, refused = anyio.run(go)

    # discovered through MCP, filtered by allowlist
    assert seen == ["get_system_time"]
    assert hidden == []
    # called through MCP
    assert ok.ok, ok.error
    assert "local" in ok.data
    # refused for an agent without permission
    assert not refused.ok
    assert refused.error_code == "denied"
    # audit records for both
    records = audit.read_all()
    assert [(r.task_id, r.agent, r.tool, r.outcome) for r in records] == [
        ("task-1", "time_tester", "get_system_time", "ok"),
        ("task-2", "router", "get_system_time", "denied"),
    ]
    assert "allowlist" in records[1].reason


class _NoArgs(BaseModel):
    pass


class _Done(BaseModel):
    done: bool = True


def _catalog_with(manifest: ToolManifest, calls: list[str]) -> CapabilityCatalog:
    def handler(_: _NoArgs) -> _Done:
        calls.append(manifest.name)
        return _Done()

    cat = default_catalog()
    cat.register(
        ToolSpec(manifest=manifest, input_model=_NoArgs, output_model=_Done, handler=handler)
    )
    return cat


def _run_in_process(
    cat: CapabilityCatalog, audit: AuditLogger, agent: AgentGrant, tool: str, **kw: Any
) -> Any:
    async def go() -> Any:
        async with McpToolClient(Client(build_server(cat))) as client:
            return await _policy(client, audit, cat).call_tool("t", agent, tool, {}, **kw)

    return anyio.run(go)


def test_approval_required_tool_blocked_when_not_approved(tmp_path: Path) -> None:
    calls: list[str] = []
    manifest = ToolManifest(
        name="move_thing",
        description="moves a thing",
        route="pc",
        permission_tier="T3",
        approval_required=True,
        side_effects="writes",
        status="approved",
    )
    cat = _catalog_with(manifest, calls)
    audit = AuditLogger(tmp_path / "audit.jsonl")
    agent = AgentGrant(agent_id="mover", tools=frozenset({"move_thing"}), max_tier="T3")

    result = _run_in_process(cat, audit, agent, "move_thing")
    assert not result.ok
    assert result.error_code == "needs_approval"
    assert calls == []  # never reached the tool
    assert audit.read_all()[-1].outcome == "needs_approval"

    approved = _run_in_process(cat, audit, agent, "move_thing", approved=True)
    assert approved.ok
    assert calls == ["move_thing"]


def test_unknown_tool_and_bad_args_are_refused_and_audited(tmp_path: Path) -> None:
    audit = AuditLogger(tmp_path / "audit.jsonl")
    cat = default_catalog()
    agent = AgentGrant(agent_id="a", tools=frozenset({"get_system_time", "nope"}), max_tier="T4")

    unknown = _run_in_process(cat, audit, agent, "nope")
    assert unknown.error_code == "unknown_tool"

    async def bad() -> Any:
        async with McpToolClient(Client(build_server(cat))) as client:
            return await _policy(client, audit, cat).call_tool(
                "t", agent, "get_system_time", {"cmd": "rm -rf /"}
            )

    bad_args = anyio.run(bad)
    assert bad_args.error_code == "invalid_arguments"
    outcomes = [r.outcome for r in audit.read_all()]
    assert outcomes == ["unknown_tool", "invalid_arguments"]
