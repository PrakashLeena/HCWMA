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

    decision: Literal["approve", "deny"]


class DecisionResponse(BaseModel):
    id: str
    decision: Literal["approve", "deny"]
