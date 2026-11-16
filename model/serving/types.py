"""Runtime-neutral request/response types for the local model (master document 5.5)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class ToolCall(BaseModel):
    """A tool call *proposed* by the model. The model never executes anything."""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class GenerateResult(BaseModel):
    text: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Timings in seconds. ttft = wall clock from request to first streamed token.
    ttft_s: float | None = None
    total_s: float = 0.0
    load_s: float = 0.0
    prompt_eval_s: float = 0.0
    eval_s: float = 0.0

    @property
    def tokens_per_s(self) -> float:
        return self.completion_tokens / self.eval_s if self.eval_s > 0 else 0.0

    @property
    def prompt_tokens_per_s(self) -> float:
        return self.prompt_tokens / self.prompt_eval_s if self.prompt_eval_s > 0 else 0.0
