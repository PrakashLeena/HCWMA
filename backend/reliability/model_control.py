"""Pause / resume the shared Qwen model from the tray (build step 18).

Pause: no new inference starts (the queue refuses, so nothing loads Qwen again), then — once
any model call in flight has finished — Ollama is told to unload the model, which gives its
~3.5 GB of RAM back. Resume: inference is allowed again; Qwen loads on the next request.

`RamGuard` (backlog item 1) does the same unload automatically when the model is idle and its
process has grown past a RAM limit, without pausing.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any, Protocol

from agents.runtime.model_queue import ModelQueue, get_model_queue
from model.serving import Backend, get_backend, load_model_config
from model.serving.runner_memory import runner_memory

log = logging.getLogger(__name__)


class ModelControl(Protocol):
    @property
    def paused(self) -> bool: ...

    def pause(self) -> None: ...

    def resume(self) -> None: ...

    def status(self) -> dict[str, Any]: ...


class OllamaModelControl:
    def __init__(self, queue: ModelQueue | None = None, backend: Backend | None = None) -> None:
        self._queue = queue
        self._backend = backend
        self.last_unload_error = ""

    @property
    def queue(self) -> ModelQueue:
        return self._queue or get_model_queue()

    @property
    def backend(self) -> Backend:
        return self._backend or get_backend()

    @property
    def paused(self) -> bool:
        return self.queue.paused

    def pause(self, wait: bool = False) -> None:
        self.queue.paused = True
        worker = threading.Thread(target=self._unload, name="hcwma-unload", daemon=True)
        worker.start()
        if wait:
            worker.join()

    def _unload(self) -> None:
        try:
            with self.queue.exclusive():  # after the model call in flight, if any
                if self.queue.paused:
                    self.backend.unload()
            self.last_unload_error = ""
        except Exception as exc:  # noqa: BLE001 - e.g. Ollama not running: nothing loaded
            self.last_unload_error = type(exc).__name__
            log.warning("unloading the model failed: %s", type(exc).__name__)

    def resume(self) -> None:
        self.queue.paused = False

    def status(self) -> dict[str, Any]:
        """{"up": bool, "version": str, "loaded": bool, "size_mb": int}. Raises if Ollama
        cannot be reached."""
        info = self.backend.runtime_info()
        loaded = list(info.get("loaded") or [])
        size = sum(int(m.get("size", 0) or 0) for m in loaded if isinstance(m, dict))
        return {
            "up": True,
            "version": str(info.get("version", "?")),
            "loaded": bool(loaded),
            "size_mb": size // (1024 * 1024),
        }


class RamGuard:
    """Backlog item 1: unload Qwen when it is idle and its process has grown too large.

    Ollama's llama-server keeps saved prompt states and context checkpoints, so its RAM can
    grow while HCWMA is used. When no model call ran for `idle_after_s` and the runner uses
    more than `limit_mb`, the model is unloaded (like Pause, but the queue stays open): the
    next request loads it again, fresh. Never during a model call.
    """

    def __init__(
        self,
        *,
        queue: ModelQueue | None = None,
        backend: Backend | None = None,
        measure: Callable[[], int] | None = None,
        limit_mb: int | None = None,
        idle_after_s: float | None = None,
    ) -> None:
        config = load_model_config()
        self._queue = queue
        self._backend = backend
        self._measure = measure or (lambda: runner_memory().private_mb)
        self.limit_mb = config.idle_unload_ram_mb if limit_mb is None else limit_mb
        self.idle_after_s = config.idle_unload_after_s if idle_after_s is None else idle_after_s
        self.unloads = 0
        self.last_mb = 0

    @property
    def queue(self) -> ModelQueue:
        return self._queue or get_model_queue()

    @property
    def backend(self) -> Backend:
        return self._backend or get_backend()

    @property
    def enabled(self) -> bool:
        return self.limit_mb > 0

    def current_mb(self) -> int:
        """The runner's RAM now in MB (0 when it cannot be read)."""
        try:
            return int(self._measure())
        except Exception:  # noqa: BLE001 - psutil access problems must not break health
            return 0

    def _in_use(self) -> bool:
        return self.queue.busy or self.queue.idle_seconds() < self.idle_after_s

    def check(self) -> int | None:
        """Unload when idle and over the limit. Returns the MB it saw when it unloaded."""
        if not self.enabled or self.queue.paused:
            return None
        if self._in_use():
            return None
        used = self._measure()
        self.last_mb = used
        if used <= self.limit_mb:
            return None
        with self.queue.exclusive():  # a request may have started meanwhile: wait for it
            if self._in_use():
                return None
            self.backend.unload()
        self.unloads += 1
        log.info("model unloaded by the idle RAM guard (%d MB > %d MB)", used, self.limit_mb)
        return used
