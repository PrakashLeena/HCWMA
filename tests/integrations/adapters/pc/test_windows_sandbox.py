"""Windows Sandbox runner: logic tested with a fake `wsb` (no VM). The real isolation proof is
`python -m integrations.adapters.pc.sandbox_proof`."""

from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from integrations.adapters.pc import windows_sandbox as ws


class FakeWsb:
    def __init__(self, *, hang: bool = False, boot_fail: bool = False, write_results: bool = True):
        self.hang, self.boot_fail, self.write_results = hang, boot_fail, write_results
        self.config = ""
        self.stopped: list[str] = []
        self.alive: set[str] = set()
        self.out_dir: Path | None = None

    def start(self, config: str) -> str:
        self.config = config
        self.alive.add("abc")
        marker = r"<SandboxFolder>C:\hcwma\out</SandboxFolder>"
        host = config.split("<HostFolder>")[3].split("</HostFolder>")[0]
        assert marker in config
        self.out_dir = Path(host)
        return "abc"

    def exec(self, sid, command, timeout):
        if command == "cmd /c exit 0":
            return subprocess.CompletedProcess([], 1 if self.boot_fail else 0)
        if self.hang:
            raise subprocess.TimeoutExpired(command, timeout)
        if self.write_results:
            (self.out_dir / "results.json").write_text(json.dumps({"passed": True, "tests_run": 1}))
        return subprocess.CompletedProcess([], 0)

    def stop(self, sid):
        self.stopped.append(sid)
        self.alive.discard(sid)

    def running(self):
        return set(self.alive)


def make(tmp_path: Path, wsb: FakeWsb, order: list[str] | None = None) -> ws.WindowsSandbox:
    python = tmp_path / "python"
    python.mkdir()
    (python / "python.exe").write_text("")

    @contextmanager
    def guard():
        if order is not None:
            order.append("unload")
        try:
            yield True
        finally:
            if order is not None:
                order.append("reload")

    return ws.WindowsSandbox(
        wsb=wsb, python_dir=python, runs_dir=tmp_path / "runs", model_guard=guard
    )


@pytest.fixture
def tool(tmp_path: Path) -> Path:
    d = tmp_path / "mytool"
    d.mkdir()
    (d / "test_x.py").write_text("import unittest\n")
    return d


def test_config_has_no_network_and_exactly_three_folders(tmp_path: Path) -> None:
    cfg = ws.build_config(tmp_path / "p", tmp_path / "t", tmp_path / "o")
    assert "<Networking>Disable</Networking>" in cfg
    assert cfg.count("<MappedFolder>") == 3
    assert cfg.count("<ReadOnly>false</ReadOnly>") == 1
    assert "<ReadOnly>false</ReadOnly></MappedFolder></MappedFolders>" in cfg  # the last one is out


def test_happy_path_reads_results_and_destroys(tmp_path: Path, tool: Path) -> None:
    wsb, order = FakeWsb(), []
    res = make(tmp_path, wsb, order).run_tool_tests(tool)
    assert res.passed and res.destroyed and wsb.stopped == ["abc"]
    assert order == ["unload", "reload"]
    assert not (tmp_path / "runs" / "active.json").exists()
    staged = [p for p in (tmp_path / "runs").glob("*/tool")][0]
    assert (staged / ws.RUNNER_NAME).is_file()


def test_timeout_still_stops_the_sandbox(tmp_path: Path, tool: Path) -> None:
    wsb = FakeWsb(hang=True)
    res = make(tmp_path, wsb).run_tool_tests(tool, timeout_s=1)
    assert res.stopped == "timeout" and not res.passed and res.destroyed and wsb.stopped == ["abc"]


def test_boot_failure_stops_the_sandbox(tmp_path: Path, tool: Path) -> None:
    wsb = FakeWsb(boot_fail=True)
    res = make(tmp_path, wsb).run_tool_tests(tool, boot_timeout_s=0.1)
    assert res.stopped == "start_failed" and res.destroyed


def test_error_during_run_still_stops_and_reloads_model(tmp_path: Path, tool: Path) -> None:
    class Boom(FakeWsb):
        def exec(self, sid, command, timeout):
            raise RuntimeError("wsb broke")

    wsb, order = Boom(), []
    with pytest.raises(RuntimeError):
        make(tmp_path, wsb, order).run_tool_tests(tool)
    assert wsb.stopped == ["abc"] and order == ["unload", "reload"]


def test_missing_results_means_not_passed(tmp_path: Path, tool: Path) -> None:
    res = make(tmp_path, FakeWsb(write_results=False)).run_tool_tests(tool)
    assert not res.passed and res.results == {}


def test_orphan_from_a_crashed_run_is_stopped(tmp_path: Path, tool: Path) -> None:
    wsb = FakeWsb()
    sandbox = make(tmp_path, wsb)
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "active.json").write_text(json.dumps({"id": "old"}))
    res = sandbox.run_tool_tests(tool)
    assert wsb.stopped[0] == "old" and any("orphan" in e for e in res.events)


def test_missing_python_refused(tmp_path: Path, tool: Path) -> None:
    sandbox = make(tmp_path, FakeWsb())
    (tmp_path / "python" / "python.exe").unlink()
    with pytest.raises(ws.SandboxUnavailable):
        sandbox.run_tool_tests(tool)


def test_oversized_or_linked_tool_folder_refused(tmp_path: Path, tool: Path, monkeypatch) -> None:
    monkeypatch.setattr(ws, "MAX_TOOL_FILES", 1)
    (tool / "b.py").write_text("")
    (tool / "c.py").write_text("")
    with pytest.raises(ws.ToolFolderRefused):
        ws.stage_tool_folder(tool, tmp_path / "stage")
    monkeypatch.setattr(ws, "_is_link", lambda p: p.name == "b.py")
    monkeypatch.setattr(ws, "MAX_TOOL_FILES", 500)
    with pytest.raises(ws.ToolFolderRefused):
        ws.stage_tool_folder(tool, tmp_path / "stage2")


def test_model_released_unloads_holds_queue_and_reloads() -> None:
    log: list[str] = []

    class Backend:
        loaded = True

        def runtime_info(self):
            return {"loaded": [1] if self.loaded else []}

        def unload(self):
            self.loaded = False
            log.append("unload")

        def load(self):
            self.loaded = True
            log.append("load")

    class Queue:
        @contextmanager
        def exclusive(self):
            log.append("lock")
            yield
            log.append("unlock")

    b = Backend()
    with ws.model_released(b, Queue()) as was:
        assert was and not b.loaded
    assert b.loaded and log == ["lock", "unload", "load", "unlock"]


def test_model_not_running_is_fine() -> None:
    class Down:
        def runtime_info(self):
            raise OSError("no ollama")

    class Queue:
        @contextmanager
        def exclusive(self):
            yield

    with ws.model_released(Down(), Queue()) as was:
        assert was is False


def test_registry_delete_refuses_any_other_path(tmp_path: Path) -> None:
    sandbox = make(tmp_path, FakeWsb())
    (tmp_path / "runs").mkdir()
    other = tmp_path / "runs" / "results.json"
    elsewhere = tmp_path / "active.json"  # right name, wrong folder
    sub = tmp_path / "runs" / "sub"
    sub.mkdir()
    nested = sub / "active.json"
    for f in (other, elsewhere, nested):
        f.write_text("keep")
        with pytest.raises(ValueError):
            sandbox._remove_registry(f)
        assert f.read_text() == "keep"
    own = tmp_path / "runs" / "active.json"
    own.write_text("{}")
    sandbox._remove_registry(own)
    assert not own.exists()
