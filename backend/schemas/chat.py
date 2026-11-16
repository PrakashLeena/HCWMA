"""Request and response models for the local `/chat` and `/health` endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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

    @model_validator(mode="after")
    def _last_turn_is_user(self) -> ChatRequest:
        if self.messages[-1].role != "user":
            raise ValueError("the last message must be from the user")
        return self


class ChatResponse(BaseModel):
    reply: str
    completion_tokens: int
    tokens_per_s: float
    total_s: float


class HealthResponse(BaseModel):
    status: Literal["ok"]
