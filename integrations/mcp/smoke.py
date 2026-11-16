"""Step 6 "Done when" check, run by hand:

    python -m integrations.mcp.smoke

Starts the local MCP server over stdio, discovers tools, calls `get_system_time` as an
allowed test agent, tries the same call as an agent with no tools (must be refused), and
prints the audit records it wrote to `security/audit/logs/audit.jsonl`.
"""

from __future__ import annotations

import uuid

import anyio
from mcp import Client

from agents.runtime.policy import PolicyLayer
from integrations.mcp.client import McpToolClient, local_server_params
from integrations.mcp.tool_schemas import PermissionTier
from integrations.registry.capability_catalog import default_catalog
from security.audit.audit_logger import get_audit_logger
from security.permissions.permission_manager import (
    AgentGrant,
    PermissionManager,
    load_permissions_config,
)

ALLOWED = AgentGrant(
    agent_id="smoke_allowed", tools=frozenset({"get_system_time"}), max_tier=PermissionTier.T0
)
NO_TOOLS = AgentGrant(agent_id="smoke_no_tools", tools=frozenset(), max_tier=PermissionTier.T0)


async def run() -> bool:
    audit = get_audit_logger()
    before = len(audit.read_all())
    task_id = f"smoke-{uuid.uuid4().hex[:8]}"
    async with McpToolClient(Client(local_server_params())) as client:
        policy = PolicyLayer(
            client=client,
            catalog=default_catalog(),
            permissions=PermissionManager(load_permissions_config()),
            audit=audit,
        )
        print("MCP discovery (all):     ", [t.name for t in await client.list_tools()])
        print("visible to smoke_allowed:", [t.name for t in await policy.list_tools_for(ALLOWED)])
        print("visible to smoke_no_tools:", [t.name for t in await policy.list_tools_for(NO_TOOLS)])
        ok = await policy.call_tool(task_id, ALLOWED, "get_system_time", {})
        print("call as smoke_allowed:   ", ok.model_dump(exclude_none=True))
        refused = await policy.call_tool(task_id, NO_TOOLS, "get_system_time", {})
        print("call as smoke_no_tools:  ", refused.model_dump(exclude_none=True))

    new = audit.read_all()[before:]
    print(f"\naudit records written to {audit.path}:")
    for rec in new:
        print("  ", rec.model_dump_json(exclude_none=True))
    passed = ok.ok and refused.error_code == "denied" and len(new) == 2
    print("\nSTEP 6 DONE-WHEN:", "PASS" if passed else "FAIL")
    return passed


def main() -> None:
    raise SystemExit(0 if anyio.run(run) else 1)


if __name__ == "__main__":
    main()
