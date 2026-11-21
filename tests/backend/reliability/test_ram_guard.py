"""Backlog item 1: the idle RAM guard unloads Qwen only when idle and over the limit."""

from __future__ import annotations

import threading
import time
from typing import Any

from agents.runtime.model_queue import ModelQueue
from backend.reliability.model_control import RamGuard
from model.serving import GenerateResult


class FakeBackend:
    def __init__(self) -> None:
        self.unloads = 0

    def unload(self) -> None:
        self.unloads += 1


def _guard(
    queue: ModelQueue, used_mb: int, limit_mb: int = 4500, idle_after_s: float = 0.0
) -> tuple[RamGuard, FakeBackend]:
    backend = FakeBackend()
    guard = RamGuard(
        queue=queue,
        backend=backend,  # type: ignore[arg-type]
        measure=lambda: used_mb,
        limit_mb=limit_mb,
        idle_after_s=idle_after_s,
    )
    return guard, backend


def _queue() -> ModelQueue:
    return ModelQueue(lambda *a, **k: GenerateResult(text="ok"))


def test_idle_and_over_the_limit_unloads() -> None:
    guard, backend = _guard(_queue(), used_mb=5700)
    assert guard.check() == 5700
    assert backend.unloads == 1 and guard.unloads == 1


def test_under_the_limit_keeps_the_model() -> None:
    guard, backend = _guard(_queue(), used_mb=3300)
    assert guard.check() is None and backend.unloads == 0
    assert guard.last_mb == 3300


def test_not_idle_long_enough_keeps_the_model() -> None:
    queue = _queue()
    queue.generate([])
    guard, backend = _guard(queue, used_mb=9000, idle_after_s=60)
    assert guard.check() is None and backend.unloads == 0


def test_never_during_a_model_call() -> None:
    started, release = threading.Event(), threading.Event()

    def slow(*_a: Any, **_k: Any) -> GenerateResult:
        started.set()
        release.wait(5)
        return GenerateResult(text="done")

    queue = ModelQueue(slow)
    worker = threading.Thread(target=lambda: queue.generate([]))
    worker.start()
    started.wait(5)
    assert queue.idle_seconds() == 0.0
    guard, backend = _guard(queue, used_mb=9000)
    assert guard.check() is None and backend.unloads == 0
    release.set()
    worker.join(5)
    time.sleep(0.05)  # Windows' monotonic clock ticks every ~16 ms
    assert queue.idle_seconds() > 0
    assert guard.check() == 9000


def test_paused_or_disabled_does_nothing() -> None:
    queue = _queue()
    queue.paused = True
    guard, backend = _guard(queue, used_mb=9000)
    assert guard.check() is None
    off, backend2 = _guard(_queue(), used_mb=9000, limit_mb=0)
    assert not off.enabled and off.check() is None
    assert backend.unloads == backend2.unloads == 0


def test_after_an_unload_the_next_request_still_runs() -> None:
    queue = _queue()
    guard, _ = _guard(queue, used_mb=9000)
    guard.check()
    assert not queue.paused and queue.generate([]).text == "ok"
