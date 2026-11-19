"""Policy layer + tool gateway (architecture rules 1, 5, 6, 7; master document 4, 9.5).

The model never executes anything. An agent proposes `(tool, arguments)`; this layer:

1. looks the tool up in the capability catalogue,
2. checks the agent's grant with the permission manager (allowlist, status, tier),
3. validates the arguments against the tool's input model,
4. if the tool needs the owner (every T3/T4 tool): pauses, shows the approval prompt and
   waits; only a one-time approval of this exact action lets it continue (build step 9),
5. sends the call to the local MCP server,
6. writes one audit record for the outcome — refused, failed or done.

It never raises to the agent; every path returns a `ToolResult`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from typing import Any

import mcp_types as types
from pydantic import ValidationError

from integrations.mcp.client import McpToolClient
from integrations.mcp.tool_schemas import CALLER_KEY, CallerContext, ToolResult, ToolSpec
from integrations.registry.capability_catalog import CapabilityCatalog
from security.approvals.approval_service import ApprovalService
from security.audit.audit_logger import AuditLogger
from security.permissions.permission_manager import AgentGrant, PermissionManager

log = logging.getLogger(__name__)


class PolicyLayer:
    def __init__(
        self,
        *,
        client: McpToolClient,
        catalog: CapabilityCatalog,
        permissions: PermissionManager,
        audit: AuditLogger,
        approvals: ApprovalService | None = None,
    ) -> None:
        self._client = client
        self._catalog = catalog
        self._permissions = permissions
        self._audit = audit
        # Without an approval service nothing that needs approval can ever run.
        self._approvals = approvals

    async def list_tools_for(self, agent: AgentGrant) -> list[types.Tool]:
        """Tools discovered over MCP, filtered to what this agent could ever call."""
        visible: list[types.Tool] = []
        for tool in await self._client.list_tools():
            spec = self._catalog.get(tool.name)
            if spec is None:
                continue
            # "needs_approval" tools are visible: the agent may propose them, the owner decides.
            if self._permissions.check(agent, spec.manifest).outcome != "denied":
                visible.append(tool)
        return visible

    async def call_tool(
        self,
        task_id: str,
        agent: AgentGrant,
        tool: str,
        arguments: dict[str, Any],
        *,
        owner_wait: Callable[[], AbstractContextManager[None]] | None = None,
        context: CallerContext | None = None,
    ) -> ToolResult:
        """`owner_wait`: entered while the call waits for the owner's answer, so the caller
        can pause the agent's own time limit (the prompt has its own, shorter one).
        `context`: trusted caller details for tools that need them (memory tools). It is
        built by the runtime from the agent's card, never from the model's arguments."""
        start = time.perf_counter()
        waited = 0.0

        def finish(result: ToolResult, reason: str = "") -> ToolResult:
            self._audit.tool_call(
                task_id=task_id,
                agent=agent.agent_id,
                tool=tool,
                args=arguments,
                outcome="ok" if result.ok else (result.error_code or "error"),
                reason=reason or (result.error or ""),
                duration_ms=(time.perf_counter() - start - waited) * 1000,
            )
            return result

        spec = self._catalog.get(tool)
        if spec is None:
            return finish(ToolResult.failure("unknown_tool", f"no tool named '{tool}'"))

        decision = self._permissions.check(agent, spec.manifest)
        if decision.outcome == "denied":
            return finish(ToolResult.failure("denied", decision.reason))

        try:
            args = spec.input_model.model_validate(arguments)
        except ValidationError as exc:
            fields = sorted({".".join(map(str, e["loc"])) or "<root>" for e in exc.errors()})
            return finish(
                ToolResult.failure("invalid_arguments", f"invalid arguments: {', '.join(fields)}")
            )

        if spec.precheck is not None:
            try:
                refusal = spec.precheck(args, context)
            except Exception:  # noqa: BLE001 - a broken rule refuses, it never lets a call pass
                log.warning("precheck() failed for tool %s", spec.name)
                refusal = "the safety check for this action could not run"
            if refusal:
                return finish(ToolResult.failure("denied", refusal))

        reason = decision.reason
        if decision.outcome == "needs_approval":
            # The only way past this point is the owner's one-time approval of exactly this
            # task, agent, tool and arguments. Nothing a caller passes in can stand in for it.
            if self._approvals is None:
                return finish(ToolResult.failure("needs_approval", decision.reason))
            with owner_wait() if owner_wait is not None else nullcontext():
                answer = await self._approvals.request(
                    task_id=task_id,
                    agent=agent.agent_id,
                    tool=tool,
                    tier=spec.manifest.permission_tier,
                    arguments=arguments,
                    summary=_describe(spec, args),
                )
            waited = answer.waited_s
            if answer.outcome == "denied":
                return finish(ToolResult.failure("approval_denied", f"the owner denied '{tool}'"))
            if answer.outcome != "approved":
                return finish(
                    ToolResult.failure(
                        "needs_approval", f"'{tool}' was not approved ({answer.outcome})"
                    )
                )
            if not self._approvals.consume(
                answer.approval_id,
                task_id=task_id,
                agent=agent.agent_id,
                tool=tool,
                arguments=arguments,
            ):
                return finish(
                    ToolResult.failure("needs_approval", f"the approval for '{tool}' is not valid")
                )
            reason = f"owner approved ({answer.approval_id[:8]})"

        sent = arguments
        if spec.caller_context:
            if context is None or context.agent_id != agent.agent_id:
                return finish(
                    ToolResult.failure("denied", f"'{tool}' needs a trusted caller context")
                )
            # The model's arguments were validated above with extra fields forbidden, so a
            # model can never supply this key itself.
            sent = {**arguments, CALLER_KEY: context.model_dump(mode="json")}
        result = await self._client.call_tool(tool, sent, timeout_s=spec.manifest.timeout_s + 5)
        return finish(result, reason=reason if result.ok else "")

    def direct_answer(self, tool: str, result: ToolResult) -> str | None:
        """The code-built answer for a successful read-only lookup, or None (see
        `ToolSpec.answer` and `AgentCard.direct_answer`)."""
        return _answer(self._catalog.get(tool), result)


def _answer(spec: ToolSpec | None, result: ToolResult) -> str | None:
    if spec is None or spec.answer is None or not result.ok or result.data is None:
        return None
    if spec.manifest.side_effects != "none" or spec.manifest.approval_required:
        return None  # only read-only lookups answer from code
    try:
        text = spec.answer(spec.output_model.model_validate(result.data))
    except Exception:  # noqa: BLE001 - fall back to the model, never fail the task
        log.warning("answer() failed for tool %s", spec.name)
        return None
    return text.strip() if text and text.strip() else None


def _describe(spec: ToolSpec, args: Any) -> str:
    """What the owner is told will happen. Falls back to the tool's own description."""
    if spec.describe is not None:
        try:
            text = spec.describe(args).strip()
            if text:
                return text
        except Exception:  # noqa: BLE001 - a broken description must not hide the prompt
            log.warning("describe() failed for tool %s", spec.name)
    return spec.manifest.description
