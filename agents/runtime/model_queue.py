"""Single shared model, queued requests (master document 5.5, architecture rule 3).

Every inference call in HCWMA goes through `ModelQueue.generate`. Requests beyond
`max_parallel_requests` (1 by default) wait their turn in FIFO order.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from model.serving import GenerateResult, Message, get_backend, load_model_config

GenerateFn = Callable[..., GenerateResult]


class ModelQueue:
    def __init__(self, generate_fn: GenerateFn, max_parallel: int = 1) -> None:
        if max_parallel < 1:
            raise ValueError("max_parallel must be >= 1")
        self._generate = generate_fn
        self._slots = threading.Semaphore(max_parallel)
        self._fifo = threading.Lock()  # keeps waiting callers in arrival order

    def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 512,
        **kwargs: Any,
    ) -> GenerateResult:
        with self._fifo:
            self._slots.acquire()
        try:
            return self._generate(messages, tools=tools, max_tokens=max_tokens, **kwargs)
        finally:
            self._slots.release()


@lru_cache(maxsize=1)
def get_model_queue() -> ModelQueue:
    config = load_model_config()
    return ModelQueue(get_backend().generate, max_parallel=config.max_parallel_requests)
