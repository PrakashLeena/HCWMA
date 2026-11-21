"""Request and response models for the approval and stop endpoints (build step 9)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from agents.runtime.stop import StopReport
from security.approvals.approval_service import PendingApproval

__all__ = ["ApprovalDecision", "DecisionResponse", "PendingApprovals", "StopReport"]


class PendingApprovals(BaseModel):
    pending: list[PendingApproval]


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # "always": only on a card that offers "Always allow" (opening an app, 2026-11-21).
    decision: Literal["approve", "deny", "always"]


class DecisionResponse(BaseModel):
    id: str
    decision: Literal["approve", "deny", "always"]


class ToolApproveRequest(BaseModel):
    """The owner approves the exact files they were shown (`hash`) for these agents."""

    model_config = ConfigDict(extra="forbid")

    hash: str
    agents: list[str]


class ToolRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hash: str
