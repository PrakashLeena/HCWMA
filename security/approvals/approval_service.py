"""Approval service (build step 9; master document 9.1, 9.3, 9.4).

A tool call that needs the owner (every T3 and T4 tool, and anything a manifest or an agent
card marks `approval_required`) pauses here. The owner sees what will happen and answers
Approve or Deny in the desktop app.

What makes an approval safe:

- **Only the owner can answer.** `decide()` is reached only from the token-protected
  backend endpoint. No agent, tool or model output can call it; saying "yes, approved" in
  the chat does nothing.
- **Bound to one exact action.** A request is tied to the task, the agent, the tool and a
  hash of the arguments. `consume()` checks all of them again right before the tool runs.
- **Single use.** A consumed approval is gone; a second call needs a new prompt.
- **Short-lived.** Nobody answering within the timeout counts as "not approved".
- **"Always allow" only where a tool offers it** (2026-11-21: opening an app). The card shows
  the third button only when the tool has a `remember` rule; the owner's click is the only way
  to pick it, and what is remembered is decided by that tool's code, never by the model.

Every request and its outcome is written to the audit log.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import anyio
from pydantic import BaseModel

from integrations.mcp.tool_schemas import PermissionTier
from security.audit.audit_logger import AuditLogger

DEFAULT_APPROVAL_TIMEOUT_S = 300.0
# Long enough to show a whole email (body limit 5000 chars) so the owner approves what is
# really sent, never a cut-off version of it.
MAX_SUMMARY_CHARS = 6500
ApprovalOutcome = Literal["approved", "denied", "expired", "cancelled"]
Decision = Literal["approve", "deny", "always"]
_State = Literal["pending", "approved", "denied", "expired", "cancelled", "used"]


def action_hash(tool: str, arguments: dict[str, Any]) -> str:
    """Stable hash of exactly what was shown to the owner."""
    canonical = json.dumps([tool, arguments], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PendingApproval(BaseModel):
    """What the owner is shown. Built by code from the validated arguments, not by the model."""

    id: str
    task_id: str
    agent: str
    tool: str
    tier: PermissionTier
    summary: str
    arguments: dict[str, Any]
    expires_in_s: int
    can_remember: bool = False  # the card offers "Always allow"


@dataclass
class _Request:
    id: str
    task_id: str
    agent: str
    tool: str
    tier: PermissionTier
    summary: str
    arguments: dict[str, Any]
    digest: str
    expires_at: float
    can_remember: bool = False
    remember: bool = False
    state: _State = "pending"
    answered: anyio.Event = field(default_factory=anyio.Event)


@dataclass(frozen=True)
class ApprovalAnswer:
    approval_id: str
    outcome: ApprovalOutcome
    waited_s: float
    remember: bool = False  # the owner clicked "Always allow"


class ApprovalJournal(Protocol):
    """`backend.reliability.journal.TaskJournal` (or a test double)."""

    def open_approval(self, approval_id: str, *, task_id: str, agent: str, tool: str) -> None: ...

    def close_approval(self, approval_id: str, outcome: str) -> None: ...


class ApprovalService:
    def __init__(
        self,
        audit: AuditLogger,
        timeout_s: float = DEFAULT_APPROVAL_TIMEOUT_S,
        journal: ApprovalJournal | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self._audit = audit
        # Build step 18: open prompts are journaled so the next start can cancel any that a
        # crash left behind (they are in memory only and can never be answered after it).
        self.journal = journal
        self._timeout_s = timeout_s
        self._requests: dict[str, _Request] = {}
        self._lock = threading.Lock()

    # ---------- asked by the policy layer ----------

    async def request(
        self,
        *,
        task_id: str,
        agent: str,
        tool: str,
        tier: PermissionTier,
        arguments: dict[str, Any],
        summary: str,
        can_remember: bool = False,
    ) -> ApprovalAnswer:
        """Show the prompt and wait for the owner. Never raises on "no"; returns the outcome."""
        req = _Request(
            id=uuid.uuid4().hex,
            task_id=task_id,
            agent=agent,
            tool=tool,
            tier=tier,
            summary=summary[:MAX_SUMMARY_CHARS],
            arguments=arguments,
            digest=action_hash(tool, arguments),
            expires_at=time.monotonic() + self._timeout_s,
            can_remember=can_remember,
        )
        with self._lock:
            self._requests[req.id] = req
        self._log(req, "requested")
        start = time.monotonic()
        try:
            with anyio.move_on_after(self._timeout_s):
                await req.answered.wait()
        except BaseException:
            # The task was stopped while waiting: the prompt must not stay answerable.
            self._close(req, "cancelled")
            raise
        if req.state == "pending":
            self._close(req, "expired")
        outcome: ApprovalOutcome = "expired"
        if req.state in ("approved", "denied", "cancelled"):
            outcome = req.state  # type: ignore[assignment]
        if outcome != "approved":
            with self._lock:
                self._requests.pop(req.id, None)
        remember = outcome == "approved" and req.remember
        return ApprovalAnswer(req.id, outcome, time.monotonic() - start, remember=remember)

    def consume(
        self, approval_id: str, *, task_id: str, agent: str, tool: str, arguments: dict[str, Any]
    ) -> bool:
        """True exactly once, and only for the same task, agent, tool and arguments."""
        with self._lock:
            req = self._requests.pop(approval_id, None)
        if req is None or req.state != "approved":
            return False
        ok = (
            time.monotonic() <= req.expires_at
            and req.task_id == task_id
            and req.agent == agent
            and req.tool == tool
            and req.digest == action_hash(tool, arguments)
        )
        req.state = "used"
        self._log(req, "used" if ok else "mismatch")
        return ok

    # ---------- answered by the owner (backend endpoint only) ----------

    def pending(self) -> list[PendingApproval]:
        now = time.monotonic()
        with self._lock:
            waiting = [r for r in self._requests.values() if r.state == "pending"]
        return [
            PendingApproval(
                id=r.id,
                task_id=r.task_id,
                agent=r.agent,
                tool=r.tool,
                tier=r.tier,
                summary=r.summary,
                arguments=r.arguments,
                expires_in_s=max(0, int(r.expires_at - now)),
                can_remember=r.can_remember,
            )
            for r in sorted(waiting, key=lambda r: r.expires_at)
            if r.expires_at > now
        ]

    def decide(self, approval_id: str, approve: bool | Decision) -> bool:
        """The owner's answer: approve (once), deny, or "always" where the card offers it.
        False if there is no such pending prompt (unknown, answered, expired or cancelled), or
        "always" on a card that does not offer it — an answer can never be changed or
        replayed."""
        if isinstance(approve, str):
            decision: Decision = approve
        else:
            decision = "approve" if approve else "deny"
        with self._lock:
            req = self._requests.get(approval_id)
            if req is None or req.state != "pending" or time.monotonic() > req.expires_at:
                return False
            if decision == "always" and not req.can_remember:
                return False
            req.state = "denied" if decision == "deny" else "approved"
            req.remember = decision == "always"
        self._log(req, "approved_always" if req.remember else req.state)
        req.answered.set()
        return True

    def cancel_all(self) -> int:
        """Stop control: every waiting prompt is withdrawn. Returns how many."""
        with self._lock:
            waiting = [r for r in self._requests.values() if r.state == "pending"]
        for req in waiting:
            self._close(req, "cancelled")
            req.answered.set()
        return len(waiting)

    # ---------- internals ----------

    def _close(self, req: _Request, state: Literal["expired", "cancelled"]) -> None:
        with self._lock:
            if req.state not in ("pending", "approved"):
                return
            req.state = state
            self._requests.pop(req.id, None)
        self._log(req, state)

    def _log(self, req: _Request, outcome: str) -> None:
        if self.journal is not None:
            try:
                if outcome == "requested":
                    self.journal.open_approval(
                        req.id, task_id=req.task_id, agent=req.agent, tool=req.tool
                    )
                else:
                    self.journal.close_approval(req.id, outcome)
            except Exception:  # noqa: BLE001 - the journal must never block an approval
                logging.getLogger(__name__).warning("task journal write failed")
        self._audit.approval(
            task_id=req.task_id,
            agent=req.agent,
            tool=req.tool,
            args=req.arguments,
            outcome=outcome,
            reason=f"approval {req.id[:8]} tier {req.tier.value}",
        )
