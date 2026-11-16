"""Local model serving. The rest of HCWMA calls `generate()` and never sees the runtime.

Agents must not call this directly: all inference goes through
`agents/runtime/model_queue.py` (architecture rule 3).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Protocol

from model.serving.config import ModelConfig, load_model_config
from model.serving.types import GenerateResult, Message, ToolCall

__all__ = [
    "Backend",
    "GenerateResult",
    "Message",
    "ModelConfig",
    "ToolCall",
    "generate",
    "get_backend",
    "load_model_config",
    "make_backend",
]


class Backend(Protocol):
    def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 512,
        response_format: dict[str, Any] | None = None,
        temperature: float | None = None,
    ) -> GenerateResult: ...

    def load(self) -> float: ...

    def unload(self) -> None: ...

    def runtime_info(self) -> dict[str, Any]: ...


def make_backend(config: ModelConfig) -> Backend:
    if config.runtime == "ollama":
        from model.serving.ollama_backend import OllamaBackend

        return OllamaBackend(config)
    raise ValueError(f"unsupported runtime {config.runtime!r}")


@lru_cache(maxsize=1)
def get_backend() -> Backend:
    """The single shared backend for this process (one model instance)."""
    return make_backend(load_model_config())


def generate(
    messages: list[Message],
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 512,
    response_format: dict[str, Any] | None = None,
    temperature: float | None = None,
) -> GenerateResult:
    return get_backend().generate(
        messages,
        tools=tools,
        max_tokens=max_tokens,
        response_format=response_format,
        temperature=temperature,
    )
