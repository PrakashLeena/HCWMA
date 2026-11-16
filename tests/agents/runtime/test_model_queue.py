"""Step 3: one model instance — the queue never runs more than N requests at once."""

import threading
import time
from typing import Any

from agents.runtime.model_queue import ModelQueue
from model.serving import GenerateResult, Message


class FakeModel:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def __call__(self, messages: list[Message], **kwargs: Any) -> GenerateResult:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(kwargs)
        time.sleep(0.02)
        with self._lock:
            self.active -= 1
        return GenerateResult(text=messages[-1].content)


def _hammer(queue: ModelQueue, n: int) -> list[str]:
    out: list[str] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        r = queue.generate([Message(role="user", content=str(i))], max_tokens=5)
        with lock:
            out.append(r.text)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def test_requests_are_serialised() -> None:
    model = FakeModel()
    results = _hammer(ModelQueue(model, max_parallel=1), 8)
    assert model.max_active == 1
    assert sorted(results, key=int) == [str(i) for i in range(8)]


def test_max_parallel_is_respected() -> None:
    model = FakeModel()
    _hammer(ModelQueue(model, max_parallel=2), 8)
    assert model.max_active <= 2


def test_arguments_are_forwarded() -> None:
    model = FakeModel()
    queue = ModelQueue(model)
    queue.generate([Message(role="user", content="x")], tools=[{"a": 1}], max_tokens=7)
    assert model.calls == [{"tools": [{"a": 1}], "max_tokens": 7}]


def test_slot_released_after_error() -> None:
    def boom(messages: list[Message], **kwargs: Any) -> GenerateResult:
        raise RuntimeError("model crashed")

    queue = ModelQueue(boom)
    for _ in range(2):
        try:
            queue.generate([Message(role="user", content="x")])
        except RuntimeError:
            pass
    ok = ModelQueue(FakeModel())
    assert ok.generate([Message(role="user", content="y")]).text == "y"
