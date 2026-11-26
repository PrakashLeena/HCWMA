"""Request and response models for the local `/chat` and `/health` endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from model.serving.usage import ModelUse

MAX_TURNS = 100
MAX_TURN_CHARS = 20000


class ChatTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # No "system" or "tool" role: the client cannot replace the server's instructions.
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_TURN_CHARS)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[ChatTurn] = Field(min_length=1, max_length=MAX_TURNS)
    # Random id the page picks per chat; groups the session summary (step 10). Optional.
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{8,64}$")

    @model_validator(mode="after")
    def _last_turn_is_user(self) -> ChatRequest:
        if self.messages[-1].role != "user":
            raise ValueError("the last message must be from the user")
        return self


class ChatResponse(BaseModel):
    reply: str
    action: Literal["delegate", "clarify", "answer"]
    status: Literal["done", "needs_approval", "needs_clarification", "failed"]
    agents: list[str]
    tool_calls: int
    stopped_by: str | None = None
    task_id: str
    total_s: float
    # Which model answered (2026-11-26): local Qwen and/or the owner's cloud model.
    models: list[ModelUse] = Field(default_factory=list)
    # e.g. "Answered by the local model: the cloud provider's rate limit was reached."
    notes: list[str] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: Literal["ok"]
