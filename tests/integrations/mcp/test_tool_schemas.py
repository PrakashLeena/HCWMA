"""Step 6: tool manifest rules (architecture rule 6, master document 6.6 and 9.4)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from integrations.mcp.tool_schemas import PermissionTier, ToolManifest


def _data(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "x_tool",
        "description": "does x",
        "route": "pc",
        "permission_tier": "T0",
        "approval_required": False,
        "side_effects": "none",
    }
    base.update(kw)
    return base


def test_valid_manifest_defaults() -> None:
    m = ToolManifest.model_validate(_data())
    assert m.permission_tier == PermissionTier.T0
    assert m.status == "quarantined"  # rule 12: new tools start quarantined
    assert m.version == "0.1.0"
    assert m.permissions.network == []


@pytest.mark.parametrize("tier", ["T3", "T4"])
def test_t3_and_t4_must_require_approval(tier: str) -> None:
    with pytest.raises(ValidationError, match="approval_required"):
        ToolManifest.model_validate(_data(permission_tier=tier, approval_required=False))


def test_external_action_must_require_approval() -> None:
    with pytest.raises(ValidationError, match="approval_required"):
        ToolManifest.model_validate(_data(side_effects="external_action"))


def test_tier_is_mandatory() -> None:
    data = _data()
    del data["permission_tier"]
    with pytest.raises(ValidationError):
        ToolManifest.model_validate(data)


def test_bad_name_rejected() -> None:
    with pytest.raises(ValidationError):
        ToolManifest.model_validate(_data(name="Bad Name!"))


def test_tiers_are_ordered() -> None:
    assert PermissionTier.T0 < PermissionTier.T1 < PermissionTier.T4
    assert PermissionTier("T2") >= PermissionTier.T2
