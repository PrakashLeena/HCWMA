"""Pause / resume the shared Qwen model from the tray (build step 18).

Pause: no new inference starts (the queue refuses, so nothing loads Qwen again), then — once
any model call in flight has finished — Ollama is told to unload the model, which gives its
~3.5 GB of RAM back. Resume: inference is allowed again; Qwen loads on the next request.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Protocol

from agents.runtime.model_queue import ModelQueue, get_model_queue
from model.serving import Backend, get_backend

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
