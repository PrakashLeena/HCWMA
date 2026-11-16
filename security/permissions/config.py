"""Load and validate `configs/permissions.yaml` (master document 9.3, 9.4)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PERMISSIONS_PATH = REPO_ROOT / "configs" / "permissions.yaml"
TIER_NAMES = ("T0", "T1", "T2", "T3", "T4")


class TierRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: str
    default: Literal["allow", "approval_required", "deny"]


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_delegation_depth: int = Field(ge=1, le=3)
    max_tool_calls_per_task: int = Field(ge=1, le=100)


class PermissionsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tiers: dict[str, TierRule]
    approved_folders: list[str]
    workspace: str
    protected_paths: list[str]
    blocked_folders: list[str]
    delete_mode: Literal["recycle_bin"]
    limits: Limits
    generated_tools_require_approval: Literal[True]

    @field_validator("tiers")
    @classmethod
    def _all_tiers(cls, tiers: dict[str, TierRule]) -> dict[str, TierRule]:
        if set(tiers) != set(TIER_NAMES):
            raise ValueError(f"tiers must be exactly {TIER_NAMES}")
        # T4 can never be loosened below "deny" without changing code and this check.
        if tiers["T4"].default != "deny":
            raise ValueError("T4 default must stay 'deny'")
        if tiers["T3"].default == "allow":
            raise ValueError("T3 default must not be 'allow'")
        return tiers


def load_permissions_config(path: Path | None = None) -> PermissionsConfig:
    with (path or DEFAULT_PERMISSIONS_PATH).open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return PermissionsConfig.model_validate(data)
