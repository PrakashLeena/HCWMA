"""Single shared model, queued requests (master document 5.5, architecture rule 3).

Every inference call in HCWMA goes through `ModelQueue.generate`. Requests beyond
`max_parallel_requests` (1 by default) wait their turn in FIFO order.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from agents.runtime.model_lock import check_free
from model.serving import GenerateResult, Message, get_backend, load_model_config

GenerateFn = Callable[..., GenerateResult]

PAUSED_MESSAGE = "the model is paused; choose Resume model in the HCWMA tray menu"


class ModelPaused(RuntimeError):
    """The owner paused the model from the tray (build step 18). Safe to show."""

    def __init__(self) -> None:
        super().__init__(PAUSED_MESSAGE)


class ModelQueue:
    def __init__(self, generate_fn: GenerateFn, max_parallel: int = 1) -> None:
        if max_parallel < 1:
            raise ValueError("max_parallel must be >= 1")
        self._generate = generate_fn
        self._max_parallel = max_parallel
        self._slots = threading.Semaphore(max_parallel)
        self._fifo = threading.Lock()  # keeps waiting callers in arrival order
        # Set by the owner's "Pause model" (step 18): no new inference starts (which would
        # load Qwen again) until "Resume model".
        self.paused = False

    def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 512,
        **kwargs: Any,
    ) -> GenerateResult:
        check_free()  # a sandbox test is running: do not start (or reload) the model
        if self.paused:
            raise ModelPaused
        with self._fifo:
            self._slots.acquire()
        try:
            check_free()  # it may have started while this request waited its turn
            if self.paused:
                raise ModelPaused
            return self._generate(messages, tools=tools, max_tokens=max_tokens, **kwargs)
        finally:
            self._slots.release()

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """Hold every slot: no inference runs (or starts) until the block ends."""
        with self._fifo:
            for _ in range(self._max_parallel):
                self._slots.acquire()
            try:
                yield
            finally:
                for _ in range(self._max_parallel):
                    self._slots.release()


@lru_cache(maxsize=1)
def get_model_queue() -> ModelQueue:
    config = load_model_config()
    return ModelQueue(get_backend().generate, max_parallel=config.max_parallel_requests)
