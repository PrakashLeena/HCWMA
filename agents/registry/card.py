"""Agent capability card: schema and loader (master document 4.4, architecture rule 4).

An agent is a card + a system prompt + a tool allowlist + memory scopes. The card is the
only place an agent's tools and tier are declared; the runtime builds the agent's
`AgentGrant` from it and nothing else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from integrations.mcp.tool_schemas import PermissionTier
from memory.scopes import KNOWN_SCOPES
from security.permissions.permission_manager import AgentGrant

AGENTS_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CARDS_DIR = AGENTS_DIR / "cards"

_ID = r"^[a-z][a-z0-9_]{0,39}$"


class CardLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Model turns this agent may take in one task (each turn may propose tool calls).
    max_steps: int = Field(default=10, ge=1, le=50)
    timeout_s: float = Field(default=120, gt=0, le=1800)


class AgentCard(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=_ID)
    name: str = Field(min_length=1, max_length=80)
    # `router` delegates, asks and answers; it may never hold tools (rule 2).
    kind: Literal["router", "worker"] = "worker"
    status: Literal["enabled", "disabled"]
    description: str = Field(min_length=1, max_length=1000)
    # One short line (about 25 words) for the Router prompt; falls back to the first handles.
    router_line: str = Field(default="", max_length=300)
    handles: list[str] = Field(default_factory=list)
    does_not_handle: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    permission_tier: PermissionTier
    approval_required: list[str] = Field(default_factory=list)
    # Memory the agent may read (master document 8; memory policy section 5).
    memory_scopes: list[str] = Field(default_factory=list)
    # Write a short episodic note when this agent finishes a task (memory policy 4).
    # Off for agents whose goals must not be repeated into memory (the Librarian: a
    # "forget X" goal would otherwise store X again).
    record_outcomes: bool = True
    system_prompt: str = Field(min_length=1)  # path relative to agents/
    limits: CardLimits = Field(default_factory=CardLimits)

    @model_validator(mode="after")
    def _consistent(self) -> AgentCard:
        if self.kind == "router":
            if self.tools:
                raise ValueError("a router card must not list any tools (rule 2)")
            if self.permission_tier != PermissionTier.T0:
                raise ValueError("a router card must have permission_tier T0")
        elif not self.handles:
            raise ValueError("a worker card needs at least one 'handles' entry for routing")
        if len(set(self.tools)) != len(self.tools):
            raise ValueError("duplicate tool in 'tools'")
        unknown = set(self.memory_scopes) - KNOWN_SCOPES
        if unknown:
            raise ValueError(f"unknown memory scopes: {sorted(unknown)}")
        extra = set(self.approval_required) - set(self.tools)
        if extra:
            raise ValueError(f"approval_required names tools not on the card: {sorted(extra)}")
        prompt = Path(self.system_prompt)
        if prompt.is_absolute() or ".." in prompt.parts:
            raise ValueError("system_prompt must be a relative path inside agents/")
        return self

    @property
    def enabled(self) -> bool:
        return self.status == "enabled"

    def grant(self) -> AgentGrant:
        """The agent's own permissions, exactly as the card declares them."""
        return AgentGrant(
            agent_id=self.id,
            tools=frozenset(self.tools),
            max_tier=self.permission_tier,
            approval_required=frozenset(self.approval_required),
        )

    def routing_line(self) -> str:
        """The single line the Router sees for this agent."""
        if self.router_line.strip():
            return self.router_line.strip()
        return "; ".join(self.handles[:3])

    def load_prompt(self, agents_dir: Path = AGENTS_DIR) -> str:
        return (agents_dir / self.system_prompt).read_text(encoding="utf-8").strip()


def load_card(path: Path) -> AgentCard:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    card = AgentCard.model_validate(data)
    if card.id != path.stem:
        raise ValueError(f"{path.name}: card id '{card.id}' must match the file name")
    return card
