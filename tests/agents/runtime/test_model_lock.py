"""Step 13: while a sandbox test runs, the backend must not start or reload the model.

The lock is a file in the HCWMA data folder, honoured by both processes."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agents.runtime import model_lock
from agents.runtime.model_lock import BUSY_MESSAGE, ModelBusy
from agents.runtime.model_queue import ModelQueue
from backend.api.app import create_app
from backend.config import BackendConfig
from integrations.adapters.pc import windows_sandbox as ws
from model.serving import GenerateResult, Message

REPO = Path(__file__).resolve().parents[3]


def _result() -> GenerateResult:
    return GenerateResult(text="hi", tool_calls=[], finish_reason="stop")  # type: ignore[call-arg]


def test_queue_refuses_inference_while_the_lock_is_held() -> None:
    calls: list[int] = []
    queue = ModelQueue(lambda *a, **k: calls.append(1) or _result())
    queue.generate([Message(role="user", content="x")])
    assert calls == [1]
    with model_lock.hold("test"):
        with pytest.raises(ModelBusy, match="busy: testing a tool"):
            queue.generate([Message(role="user", content="x")])
    queue.generate([Message(role="user", content="x")])  # released: works again
    assert calls == [1, 1]


def test_lock_taken_while_a_request_waits_is_still_honoured() -> None:
    queue = ModelQueue(lambda *a, **k: pytest.fail("model must not start"))
    with queue._fifo:  # noqa: SLF001 - simulate the request waiting for its turn
        pass

    # the lock appears between the first check and the generate call
    original = model_lock.check_free
    state = {"n": 0}

    def late_lock() -> None:
        state["n"] += 1
        if state["n"] == 2:
            raise ModelBusy
        original()

    import agents.runtime.model_queue as mq

    mq_check = mq.check_free
    mq.check_free = late_lock  # type: ignore[assignment]
    try:
        with pytest.raises(ModelBusy):
            queue.generate([Message(role="user", content="x")])
    finally:
        mq.check_free = mq_check  # type: ignore[assignment]


def test_stale_or_released_lock_does_not_block() -> None:
    with model_lock.hold("test"):
        assert model_lock.is_busy()
        assert not model_lock.is_busy(now=time.time() + 10_000)  # expired
    assert not model_lock.is_busy()  # released (file kept, never deleted)
    assert model_lock.lock_path().is_file()
    model_lock.lock_path().write_text(
        json.dumps({"active": True, "pid": 2**22 + 12345, "until": time.time() + 999}),
        encoding="utf-8",
    )
    assert not model_lock.is_busy()  # owner process is gone: a crash cannot block the model


def test_a_second_process_holding_the_lock_blocks_this_one(tmp_path: Path) -> None:
    code = (
        "import time\n"
        "from agents.runtime import model_lock\n"
        "with model_lock.hold('child'):\n"
        "    print('held', flush=True)\n"
        "    time.sleep(30)\n"
    )
    child = subprocess.Popen(  # noqa: S603 - fixed argv
        [sys.executable, "-c", code],
        cwd=REPO,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "held"
        queue = ModelQueue(lambda *a, **k: pytest.fail("model must not start"))
        with pytest.raises(ModelBusy):
            queue.generate([Message(role="user", content="x")])
    finally:
        child.kill()
        child.wait()
    assert not model_lock.is_busy()  # the holder died: lock no longer counts


def test_chat_gets_a_busy_answer_during_a_run() -> None:
    @asynccontextmanager
    async def factory() -> AsyncIterator[Any]:
        class Handler:
            async def handle(self, conversation: list[Message]) -> Any:
                pytest.fail("the router must not run while a tool is being tested")

        yield Handler()

    app = create_app(factory, "tok", BackendConfig())
    body = {"messages": [{"role": "user", "content": "hello"}]}
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        with model_lock.hold("test"):
            response = client.post("/chat", json=body, headers={"Authorization": "Bearer tok"})
    assert response.status_code == 503 and response.json()["detail"] == BUSY_MESSAGE


def test_model_released_holds_the_lock_until_after_the_reload() -> None:
    seen: list[tuple[str, bool]] = []

    class Backend:
        loaded = True

        def runtime_info(self) -> dict[str, Any]:
            return {"loaded": [1] if self.loaded else []}

        def unload(self) -> None:
            seen.append(("unload", model_lock.is_busy()))
            self.loaded = False

        def load(self) -> None:
            seen.append(("load", model_lock.is_busy()))
            self.loaded = True

    class Queue:
        @contextmanager
        def exclusive(self) -> Any:
            yield

    with ws.model_released(Backend(), Queue()):
        assert model_lock.is_busy()  # during the run
    assert seen == [("unload", True), ("load", True)]  # still locked while reloading
    assert not model_lock.is_busy()  # released only afterwards
