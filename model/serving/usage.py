"""Which model answered a chat request (2026-11-26): local Qwen or the owner's cloud model.

The backend opens a `collect()` block around one chat request; every model call inside it
(the Router and each agent, also in child tasks: they share the list) is recorded with
`record()`. Outside a block, `record()` does nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from pydantic import BaseModel

from model.serving.types import GenerateResult


class ModelUse(BaseModel):
    where: str  # "local" or "cloud"
    provider: str
    model: str
    calls: int = 0
    cost_usd: float = 0.0


class RequestUsage(BaseModel):
    models: list[ModelUse] = []
    notes: list[str] = []

    def add(self, result: GenerateResult) -> None:
        where = "cloud" if result.is_cloud else "local"
        for use in self.models:
            if (use.provider, use.model) == (result.provider, result.model):
                break
        else:
            use = ModelUse(where=where, provider=result.provider, model=result.model)
            self.models.append(use)
        use.calls += 1
        use.cost_usd = round(use.cost_usd + result.cost_usd, 6)
        if result.fallback_note and result.fallback_note not in self.notes:
            self.notes.append(result.fallback_note)


_current: ContextVar[RequestUsage | None] = ContextVar("hcwma_model_usage", default=None)


@contextmanager
def collect() -> Iterator[RequestUsage]:
    usage = RequestUsage()
    token = _current.set(usage)
    try:
        yield usage
    finally:
        _current.reset(token)


def record(result: GenerateResult) -> None:
    usage = _current.get()
    if usage is not None:
        usage.add(result)
