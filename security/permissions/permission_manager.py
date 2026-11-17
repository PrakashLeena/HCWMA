"""Permission manager: agent allowlists × tool tiers (master document 9.3, 9.4, 9.5).

Rules, in order — the first that fails decides:
1. The tool must be on the agent's allowlist (rule 5).
2. The tool must be `approved` (rule 12: quarantined / disabled tools are never callable).
3. The tool's tier must not exceed the agent's maximum tier.
4. The tier default from `configs/permissions.yaml`: `deny` refuses, `approval_required`
   needs an owner approval, `allow` passes. A manifest or an agent card that marks the tool
   `approval_required` also needs approval whatever its tier.

T4 keeps the default `deny` (owner decision 2026-11-17): a T4 tool never comes back
"allowed" from here. The only way it runs is `needs_approval` followed by a one-time owner
approval of that exact action, which the policy layer obtains from the approval service
(`security/approvals/approval_service.py`). This class takes no "approved" flag on purpose:
no caller can claim an approval.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from integrations.mcp.tool_schemas import PermissionTier, ToolManifest
from security.permissions.config import PermissionsConfig, load_permissions_config

__all__ = [
    "AgentGrant",
    "PermissionDecision",
    "PermissionManager",
    "load_permissions_config",
]


class AgentGrant(BaseModel):
    """What one agent may use. Built from its capability card (Step 7)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    tools: frozenset[str]
    max_tier: PermissionTier
    # Tools the agent's card says always need the owner, whatever their tier.
    approval_required: frozenset[str] = frozenset()

    def delegated_from(self, parent: AgentGrant) -> AgentGrant:
        """Delegation never raises privilege: keep the lower of the two grants (9.5)."""
        return AgentGrant(
            agent_id=self.agent_id,
            tools=self.tools & parent.tools,
            max_tier=min(self.max_tier, parent.max_tier),
            approval_required=self.approval_required | parent.approval_required,
        )


class PermissionDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: Literal["allowed", "denied", "needs_approval"]
    reason: str

    @property
    def allowed(self) -> bool:
        return self.outcome == "allowed"


class PermissionManager:
    def __init__(self, config: PermissionsConfig) -> None:
        self._config = config

    def check(self, agent: AgentGrant, manifest: ToolManifest) -> PermissionDecision:
        name = manifest.name
        if name not in agent.tools:
            return _deny(f"tool '{name}' is not on agent '{agent.agent_id}' allowlist")
        if manifest.status != "approved":
            return _deny(f"tool '{name}' is {manifest.status}")
        tier = manifest.permission_tier
        if tier > agent.max_tier:
            return _deny(
                f"tool '{name}' is tier {tier.value}, above agent max tier {agent.max_tier.value}"
            )
        default = self._config.tiers[tier.value].default
        if default == "deny":
            if tier != PermissionTier.T4:
                return _deny(f"tier {tier.value} tools are denied by default")
            return PermissionDecision(
                outcome="needs_approval",
                reason=(
                    f"tool '{name}' is tier T4: denied unless the owner approves this exact "
                    "action, once"
                ),
            )
        if (
            default == "approval_required"
            or manifest.approval_required
            or name in agent.approval_required
        ):
            return PermissionDecision(
                outcome="needs_approval", reason=f"tool '{name}' needs owner approval"
            )
        return PermissionDecision(outcome="allowed", reason=f"tier {tier.value} allowed")


def _deny(reason: str) -> PermissionDecision:
    return PermissionDecision(outcome="denied", reason=reason)
