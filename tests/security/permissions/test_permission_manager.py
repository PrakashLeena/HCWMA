"""Step 6: permission manager — allowlists and tiers T0–T4 (master document 9.3, 9.4, 9.5)."""

from __future__ import annotations

import pytest

from integrations.mcp.tool_schemas import PermissionTier, ToolManifest
from security.permissions.permission_manager import (
    AgentGrant,
    PermissionManager,
    load_permissions_config,
)


def _manifest(name: str, tier: PermissionTier, **kw: object) -> ToolManifest:
    data: dict[str, object] = {
        "name": name,
        "description": "test tool",
        "route": "pc",
        "permission_tier": tier,
        "approval_required": tier >= PermissionTier.T3,
        "side_effects": "none",
        "status": "approved",
    }
    data.update(kw)
    return ToolManifest.model_validate(data)


@pytest.fixture
def pm() -> PermissionManager:
    return PermissionManager(load_permissions_config())


def test_allowed_tool_on_allowlist(pm: PermissionManager) -> None:
    agent = AgentGrant(agent_id="a", tools=frozenset({"t0"}), max_tier=PermissionTier.T1)
    decision = pm.check(agent, _manifest("t0", PermissionTier.T0))
    assert decision.allowed
    assert decision.outcome == "allowed"


def test_tool_not_on_allowlist_is_refused(pm: PermissionManager) -> None:
    agent = AgentGrant(agent_id="a", tools=frozenset(), max_tier=PermissionTier.T4)
    decision = pm.check(agent, _manifest("t0", PermissionTier.T0))
    assert not decision.allowed
    assert decision.outcome == "denied"
    assert "allowlist" in decision.reason


def test_tool_above_agent_tier_is_refused(pm: PermissionManager) -> None:
    agent = AgentGrant(agent_id="a", tools=frozenset({"t2"}), max_tier=PermissionTier.T1)
    decision = pm.check(agent, _manifest("t2", PermissionTier.T2))
    assert not decision.allowed
    assert "tier" in decision.reason


def test_quarantined_tool_is_refused(pm: PermissionManager) -> None:
    agent = AgentGrant(agent_id="a", tools=frozenset({"q"}), max_tier=PermissionTier.T4)
    decision = pm.check(agent, _manifest("q", PermissionTier.T0, status="quarantined"))
    assert not decision.allowed
    assert "quarantined" in decision.reason


def test_t3_needs_approval_and_passes_once_approved(pm: PermissionManager) -> None:
    agent = AgentGrant(agent_id="a", tools=frozenset({"w"}), max_tier=PermissionTier.T3)
    tool = _manifest("w", PermissionTier.T3)
    blocked = pm.check(agent, tool)
    assert not blocked.allowed
    assert blocked.outcome == "needs_approval"
    assert pm.check(agent, tool, approved=True).allowed


def test_t4_is_denied_by_default_even_with_approval(pm: PermissionManager) -> None:
    agent = AgentGrant(agent_id="a", tools=frozenset({"d"}), max_tier=PermissionTier.T4)
    tool = _manifest("d", PermissionTier.T4)
    for approved in (False, True):
        decision = pm.check(agent, tool, approved=approved)
        assert not decision.allowed
        assert decision.outcome == "denied"


def test_low_tier_tool_marked_approval_required_still_needs_approval(
    pm: PermissionManager,
) -> None:
    agent = AgentGrant(agent_id="a", tools=frozenset({"s"}), max_tier=PermissionTier.T2)
    tool = _manifest("s", PermissionTier.T1, approval_required=True)
    assert pm.check(agent, tool).outcome == "needs_approval"


def test_delegation_never_raises_privilege() -> None:
    parent = AgentGrant(agent_id="p", tools=frozenset({"a", "b"}), max_tier=PermissionTier.T1)
    child = AgentGrant(agent_id="c", tools=frozenset({"b", "c"}), max_tier=PermissionTier.T3)
    merged = child.delegated_from(parent)
    assert merged.agent_id == "c"
    assert merged.tools == frozenset({"b"})
    assert merged.max_tier == PermissionTier.T1


def test_config_loads_strict_defaults() -> None:
    cfg = load_permissions_config()
    assert cfg.tiers["T3"].default == "approval_required"
    assert cfg.tiers["T4"].default == "deny"
    assert cfg.limits.max_tool_calls_per_task == 20
