"""Owner decision 2026-11-20: start the installed Ollama when it is down (process start mocked)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.config import BackendConfig
from backend.reliability.service import ReliabilityService
from model.serving.config import load_model_config
from model.serving.ollama_launcher import OllamaLauncher, find_ollama
from security.audit.audit_logger import AuditLogger
from tests.backend.api.test_app import AUTH, BASE_URL, TOKEN, FakeRouter

EXE = Path("C:/Users/owner/AppData/Local/Programs/Ollama/ollama.exe")


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class FakeProc:
    def __init__(self, exit_code: int | None = None) -> None:
        self.pid = 4242
        self.exit_code = exit_code

    def poll(self) -> int | None:
        return self.exit_code


class World:
    """Ollama that answers `up_after` probes after it was started (None: never answers)."""

    def __init__(self, up: bool = False, up_after: int | None = 3, exit_code: int | None = None):
        self.up = up
        self.up_after = up_after
        self.exit_code = exit_code
        self.starts: list[tuple[list[str], dict[str, str], Path]] = []
        self.probes_since_start = 0
        self.lock = threading.Lock()

    def probe(self) -> bool:
        with self.lock:
            if self.up:
                return True
            if self.starts:
                self.probes_since_start += 1
                if self.up_after is not None and self.probes_since_start >= self.up_after:
                    self.up = True
            return self.up

    def start(self, argv: list[str], env: dict[str, str], cwd: Path) -> FakeProc:
        self.starts.append((argv, env, cwd))
        return FakeProc(self.exit_code)


def launcher(world: World, clock: Clock | None = None, exe: Path | None = EXE) -> OllamaLauncher:
    clock = clock or Clock()
    return OllamaLauncher(
        load_model_config(),
        probe=world.probe,
        finder=lambda: exe,
        starter=world.start,
        clock=clock,
        sleep=clock.sleep,
    )


def test_running_ollama_is_left_alone() -> None:
    world = World(up=True)
    result = launcher(world).ensure_running()
    assert result.ok and not result.started and world.starts == []


def test_down_ollama_is_started_hidden_on_localhost_with_the_models_folder() -> None:
    world = World(up=False, up_after=3)
    ol = launcher(world)
    result = ol.ensure_running()
    assert result.ok and result.started and result.seconds == 2.0
    assert len(world.starts) == 1
    argv, env, cwd = world.starts[0]
    assert argv == [str(EXE), "serve"] and cwd == EXE.parent
    assert env["OLLAMA_HOST"] == "127.0.0.1:11434"  # never 0.0.0.0 (rule 8)
    assert env["OLLAMA_MODELS"] == "E:/ollama/models"
    assert ol.state == "up" and ol.health_value() == "started by HCWMA"


def test_not_installed_gives_one_clear_message_and_installs_nothing() -> None:
    world = World(up=False)
    result = launcher(world, exe=None).ensure_running()
    assert not result.ok and world.starts == []
    msg = result.message()
    assert "not installed" in msg and "does not download or install" in msg
    assert "Start Ollama from the Start menu" in msg


def test_ollama_that_exits_at_once_is_reported() -> None:
    world = World(up=False, up_after=None, exit_code=1)
    result = launcher(world).ensure_running()
    assert not result.ok and "exit code 1" in result.reason


def test_gives_up_after_60_seconds_never_loops() -> None:
    world, clock = World(up=False, up_after=None), Clock()
    ol = launcher(world, clock)
    result = ol.ensure_running()
    assert not result.ok and "within 60 s" in result.reason
    assert clock.t == pytest.approx(60.0)
    assert world.probes_since_start <= 62
    assert ol.state == "failed" and "did not answer" in ol.health_value()


def test_a_failed_start_is_not_retried_at_once() -> None:
    world, clock = World(up=False, up_after=None), Clock()
    ol = launcher(world, clock)
    first = ol.ensure_running()
    second = ol.ensure_running()  # e.g. the next request
    assert second == first and len(world.starts) == 1
    clock.t += 61  # a minute later it may try again
    ol.ensure_running()
    assert len(world.starts) == 2


def test_only_one_start_even_with_many_requests() -> None:
    world = World(up=False, up_after=50)
    real = launcher(world)
    real._sleep = lambda s: None  # noqa: SLF001 - keep the waiting real-time but fast
    results: list[Any] = []
    threads = [
        threading.Thread(target=lambda: results.append(real.ensure_running())) for _ in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(results) == 6 and all(r.ok for r in results)
    assert len(world.starts) == 1


def test_find_ollama_uses_path_then_the_standard_folder(tmp_path: Path) -> None:
    exe = tmp_path / "Programs" / "Ollama" / "ollama.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert find_ollama(which=lambda _: "D:/tools/ollama.exe", candidates=lambda: [exe]) == Path(
        "D:/tools/ollama.exe"
    )
    assert find_ollama(which=lambda _: None, candidates=lambda: [tmp_path / "x.exe", exe]) == exe
    assert find_ollama(which=lambda _: None, candidates=lambda: [tmp_path / "x.exe"]) is None


def test_windows_start_is_hidden_and_detached(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from integrations.adapters.pc import windows_process

    seen: dict[str, Any] = {}

    def fake_popen(argv: list[str], **kw: Any) -> FakeProc:
        seen.update(kw, argv=argv)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    windows_process.start_hidden_detached(
        [str(EXE), "serve"], {"OLLAMA_HOST": "127.0.0.1:11434"}, EXE.parent
    )
    assert seen["argv"] == [str(EXE), "serve"]
    assert seen["creationflags"] & windows_process.DETACHED_PROCESS
    assert seen["startupinfo"].wShowWindow == windows_process.SW_HIDE
    assert seen["stdout"] is subprocess.DEVNULL and seen.get("shell") is None


# ---- through the backend: chat waits for the start, or gets one clear message ----


@pytest.fixture
def router() -> FakeRouter:
    return FakeRouter()


def _client(router: FakeRouter, ol: OllamaLauncher) -> Iterator[TestClient]:
    app = create_app(handler_factory=router.factory, token=TOKEN, config=BackendConfig(), ollama=ol)
    with TestClient(app, base_url=BASE_URL) as c:
        yield c


def test_chat_starts_ollama_then_continues(router: FakeRouter) -> None:
    world = World(up=False, up_after=2)
    ol = launcher(world)
    for c in _client(router, ol):
        r = c.post("/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=AUTH)
        assert r.status_code == 200 and router.calls
        assert c.get("/model/state", headers=AUTH).json()["state"] == "up"
    assert len(world.starts) == 1  # launch and chat shared the one start


def test_chat_gets_one_clear_message_when_ollama_cannot_start(router: FakeRouter) -> None:
    world = World(up=False)
    ol = launcher(world, exe=None)
    for c in _client(router, ol):
        r = c.post("/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=AUTH)
        assert r.status_code == 503
        assert "could not be started" in r.json()["detail"] and router.calls == []
        assert c.get("/model/state", headers=AUTH).json()["state"] == "failed"


def test_health_page_shows_the_start(tmp_path: Path) -> None:
    class DownModel:
        paused = False

        def status(self) -> dict[str, Any]:
            raise ConnectionRefusedError

    world = World(up=False)
    ol = launcher(world, exe=None)
    ol.ensure_running()
    service = ReliabilityService(
        data_dir=tmp_path / "d",
        audit=AuditLogger(tmp_path / "a.jsonl"),
        model=DownModel(),  # type: ignore[arg-type]
        probes={},
        autostart_enabled=lambda: False,
        auto_backup=False,
        ollama=ol,
    )
    item = service._model_items()[0]  # noqa: SLF001
    assert item.name == "Ollama" and item.ok is False and "not installed" in item.value


def test_the_started_ollama_gets_bounded_memory_settings() -> None:
    # Backlog item 1: one slot, one model, small prompt cache, few context checkpoints.
    config = load_model_config()
    env = OllamaLauncher(config, probe=lambda: True).environment()
    assert env["OLLAMA_NUM_PARALLEL"] == "1"
    assert env["OLLAMA_MAX_LOADED_MODELS"] == "1"
    assert env["LLAMA_ARG_CACHE_RAM"] == str(config.prompt_cache_mb)
    assert env["LLAMA_ARG_CTX_CHECKPOINTS"] == str(config.context_checkpoints)
    assert env["OLLAMA_HOST"] == "127.0.0.1:11434"
