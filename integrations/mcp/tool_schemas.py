"""Tool contract base classes (architecture rule 6, master document 6.5, 6.6 and 9.4).

Every tool declares a manifest (tier, side effects, approval, permissions), a Pydantic input
model, a Pydantic output model and a handler. The gateway validates arguments against the
input model before anything runs.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

import mcp_types as types
from pydantic import BaseModel, ConfigDict, Field, model_validator


class PermissionTier(str, Enum):
    """PC access tiers from master document 9.4, ordered T0 (least) to T4 (most)."""

    T0 = "T0"
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"
    T4 = "T4"

    @property
    def rank(self) -> int:
        return int(self.value[1])

    def _other_rank(self, other: object) -> int:
        return PermissionTier(other).rank

    def __lt__(self, other: object) -> bool:  # type: ignore[override]
        return self.rank < self._other_rank(other)

    def __le__(self, other: object) -> bool:  # type: ignore[override]
        return self.rank <= self._other_rank(other)

    def __gt__(self, other: object) -> bool:  # type: ignore[override]
        return self.rank > self._other_rank(other)

    def __ge__(self, other: object) -> bool:  # type: ignore[override]
        return self.rank >= self._other_rank(other)


ToolStatus = Literal["approved", "quarantined", "disabled"]
ToolRoute = Literal["pc", "official_api", "custom_api", "browser", "desktop", "local_data"]
SideEffects = Literal["none", "writes", "external_action"]
ErrorCode = Literal[
    "denied",
    "needs_approval",  # nobody approved it (no prompt possible, or the prompt ran out)
    "approval_denied",  # the owner said no
    "unknown_tool",
    "invalid_arguments",
    "timeout",
    "tool_error",
    "transport_error",
]


class ToolPermissions(BaseModel):
    """Everything a tool may touch. A tool cannot grant itself more (master document 6.6)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    network: list[str] = Field(default_factory=list)
    filesystem: list[str] = Field(default_factory=list)
    secrets: list[str] = Field(default_factory=list)


class ToolManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    version: str = "0.1.0"
    created_by: str = "developer"
    # New tools start quarantined; the owner approves the manifest (rule 12).
    status: ToolStatus = "quarantined"
    route: ToolRoute
    description: str = Field(min_length=1, max_length=500)
    permission_tier: PermissionTier
    approval_required: bool = False
    side_effects: SideEffects = "none"
    permissions: ToolPermissions = Field(default_factory=ToolPermissions)
    timeout_s: float = Field(default=10.0, gt=0, le=300)

    @model_validator(mode="after")
    def _high_impact_needs_approval(self) -> ToolManifest:
        if self.permission_tier >= PermissionTier.T3 and not self.approval_required:
            raise ValueError(f"{self.permission_tier.value} tools must set approval_required")
        if self.side_effects == "external_action" and not self.approval_required:
            raise ValueError("external_action tools must set approval_required")
        return self


CALLER_KEY = "_caller"


class CallerContext(BaseModel):
    """Who is calling, filled in by the policy layer from trusted state (the agent's card
    and the owner's own message), never from the model's arguments. Only tools that set
    `ToolSpec.caller_context` receive it (build step 10: the memory tools)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    task_id: str
    memory_scopes: tuple[str, ...] = ()
    # The owner's latest chat message, exactly as typed. "Remember that ..." entries must
    # be made of these words (memory policy section 4).
    owner_words: str = ""


@dataclass(frozen=True)
class ToolSpec:
    """A registered tool: manifest + validated I/O models + the handler that does the work."""

    manifest: ToolManifest
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: Callable[[Any], BaseModel]
    # Plain-language "what will happen" for the approval prompt, built from the validated
    # arguments by code (never by the model). Must not change anything.
    describe: Callable[[Any], str] | None = None
    # True: the handler is called as `handler(args, caller)` with a `CallerContext`.
    caller_context: bool = False
    # Read-only tools only: the owner-facing answer for a successful result, built by code
    # from the validated output, or None when the agent's model should decide what comes
    # next (nothing found, needs a follow-up). Used when the agent's card allows it.
    answer: Callable[[Any], str | None] | None = None
    # A rule the gateway applies BEFORE the owner is asked (build step 15): given the validated
    # arguments and the trusted caller context, return a refusal text or None. Code only, no
    # model; used so an action's target must come from the owner's own words, not from text
    # the agent read. A tool with a precheck always gets the caller context.
    precheck: Callable[[Any, CallerContext | None], str | None] | None = None
    # Per-call owner question (2026-11-21, launch_app): given the validated arguments, True
    # when THIS call needs the owner's approval even though the tier alone would not. Code
    # only; an error counts as True (fail closed).
    ask_owner: Callable[[Any], bool] | None = None
    # When set, the approval card also offers "Always allow"; the gateway calls this with the
    # validated arguments after the owner clicked it (and the approval was consumed).
    remember: Callable[[Any], None] | None = None

    @property
    def name(self) -> str:
        return self.manifest.name

    def mcp_tool(self) -> types.Tool:
        return types.Tool(
            name=self.manifest.name,
            description=self.manifest.description,
            input_schema=self.input_model.model_json_schema(),
            output_schema=ToolResult.model_json_schema(),
        )


class ToolResult(BaseModel):
    """What every tool call returns to an agent. Errors are values, never raw exceptions."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    data: dict[str, Any] | None = None
    error: str | None = None
    error_code: ErrorCode | None = None

    @classmethod
    def success(cls, output: BaseModel) -> ToolResult:
        return cls(ok=True, data=output.model_dump(mode="json"))

    @classmethod
    def failure(cls, code: ErrorCode, message: str) -> ToolResult:
        return cls(ok=False, error=message, error_code=code)

    def to_text(self) -> str:
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False)
