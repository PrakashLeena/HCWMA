"""Task envelope: the Router ↔ sub-agent contract (master document 4.5)."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TaskStatus = Literal["done", "needs_approval", "needs_clarification", "failed"]


def new_task_id() -> str:
    return uuid.uuid4().hex


class TaskContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_message: str = ""
    memory_refs: list[str] = Field(default_factory=list)


class TaskConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_steps: int = Field(default=10, ge=1, le=50)
    # 0 = the Router itself; each delegation adds one.
    depth: int = Field(default=1, ge=0)
    # read_only caps the sub-agent at T1 (no writes), whatever its card allows.
    read_only: bool = True


class ToolCallRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    ok: bool
    error_code: str | None = None


class TaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: TaskStatus
    summary: str
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    # set when the task was stopped by a limit (depth, tool-call budget, steps, timeout)
    stopped_by: str | None = None


class TaskEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(default_factory=new_task_id)
    parent_task_id: str | None = None
    # The root of the delegation tree; the tool-call budget is shared across it.
    root_task_id: str
    from_agent: str
    to_agent: str
    goal: str = Field(min_length=1, max_length=2000)
    context: TaskContext = Field(default_factory=TaskContext)
    constraints: TaskConstraints = Field(default_factory=TaskConstraints)
    result: TaskResult | None = None
