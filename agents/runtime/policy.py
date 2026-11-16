"""Policy layer + tool gateway (architecture rules 1, 5, 6, 7; master document 4, 9.5).

The model never executes anything. An agent proposes `(tool, arguments)`; this layer:

1. looks the tool up in the capability catalogue,
2. checks the agent's grant with the permission manager (allowlist, status, tier, approval),
3. validates the arguments against the tool's input model,
4. sends the call to the local MCP server,
5. writes one audit record for the outcome — refused, failed or done.

It never raises to the agent; every path returns a `ToolResult`.
"""

from __future__ import annotations

import time
from typing import Any

import mcp_types as types
from pydantic import ValidationError

from integrations.mcp.client import McpToolClient
from integrations.mcp.tool_schemas import ToolResult
from integrations.registry.capability_catalog import CapabilityCatalog
from security.audit.audit_logger import AuditLogger
from security.permissions.permission_manager import AgentGrant, PermissionManager


class PolicyLayer:
    def __init__(
        self,
        *,
        client: McpToolClient,
        catalog: CapabilityCatalog,
        permissions: PermissionManager,
        audit: AuditLogger,
    ) -> None:
        self._client = client
        self._catalog = catalog
        self._permissions = permissions
        self._audit = audit

    async def list_tools_for(self, agent: AgentGrant) -> list[types.Tool]:
        """Tools discovered over MCP, filtered to what this agent could ever call."""
        visible: list[types.Tool] = []
        for tool in await self._client.list_tools():
            spec = self._catalog.get(tool.name)
            if spec is None:
                continue
            if self._permissions.check(agent, spec.manifest, approved=True).allowed:
                visible.append(tool)
        return visible

    async def call_tool(
        self,
        task_id: str,
        agent: AgentGrant,
        tool: str,
        arguments: dict[str, Any],
        *,
        approved: bool = False,
    ) -> ToolResult:
        start = time.perf_counter()

        def finish(result: ToolResult, reason: str = "") -> ToolResult:
            self._audit.tool_call(
                task_id=task_id,
                agent=agent.agent_id,
                tool=tool,
                args=arguments,
                outcome="ok" if result.ok else (result.error_code or "error"),
                reason=reason or (result.error or ""),
                duration_ms=(time.perf_counter() - start) * 1000,
            )
            return result

        spec = self._catalog.get(tool)
        if spec is None:
            return finish(ToolResult.failure("unknown_tool", f"no tool named '{tool}'"))

        decision = self._permissions.check(agent, spec.manifest, approved=approved)
        if not decision.allowed:
            code = "needs_approval" if decision.outcome == "needs_approval" else "denied"
            return finish(ToolResult.failure(code, decision.reason))

        try:
            spec.input_model.model_validate(arguments)
        except ValidationError as exc:
            fields = sorted({".".join(map(str, e["loc"])) or "<root>" for e in exc.errors()})
            return finish(
                ToolResult.failure("invalid_arguments", f"invalid arguments: {', '.join(fields)}")
            )

        result = await self._client.call_tool(
            tool, arguments, timeout_s=spec.manifest.timeout_s + 5
        )
        return finish(result, reason=decision.reason if result.ok else "")
