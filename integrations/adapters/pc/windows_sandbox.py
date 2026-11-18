"""Real isolation for Tool Builder tests: Windows Sandbox through the `wsb` CLI (build step 13).

The Coding agent keeps the in-process guardrail runner (`sandbox_runner.py`). Code written by
the Tool Builder is run here instead, in a throw-away Windows Sandbox virtual machine:

- networking disabled, clipboard / audio / video / printers / GPU off;
- exactly three host folders are mapped: a portable Python (read-only), a snapshot copy of
  the tool's folder (read-only) and one fresh output folder (writable);
- `wsb exec` returns no program output, so the runner inside writes `results.json` to the
  output folder, and the host reads it as untrusted data (architecture rule 13);
- the Qwen model is unloaded (and the model queue held) before the VM starts and reloaded
  after it is gone: the model and the VM never run at the same time (RAM);
- a timeout, and the VM is always stopped (`finally`), also after an error. A registry file
  lets the next run stop a VM left behind by a crash.

The processes in the VM run as SYSTEM (`wsb exec` offers only System or the logged-in user,
and no user is logged in yet): the isolation is the VM boundary, not the account.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNNER_SOURCE = Path(__file__).resolve().with_name("sandbox_vm_runner.py")
SANDBOX_HOME = REPO_ROOT / "workspace" / "sandbox"
DEFAULT_PYTHON_DIR = SANDBOX_HOME / "python"
RUNS_DIR = SANDBOX_HOME / "runs"

GUEST_PYTHON = r"C:\hcwma\python"
GUEST_TOOL = r"C:\hcwma\tool"
GUEST_OUT = r"C:\hcwma\out"
RUNNER_NAME = "_hcwma_vm_runner.py"
REGISTRY_NAME = "active.json"

MAX_TOOL_FILES = 500
MAX_TOOL_BYTES = 5_000_000
MAX_RESULT_BYTES = 200_000
MEMORY_MB = 2048

Run = Callable[..., subprocess.CompletedProcess[str]]


class SandboxUnavailable(RuntimeError):
    """The sandbox cannot be used (wsb missing, Python folder missing, model still loaded...)."""


class ToolFolderRefused(ValueError):
    """The tool folder cannot be mapped into the sandbox. Message is safe to show."""


@dataclass
class SandboxRunResult:
    passed: bool
    stopped: str | None  # "timeout" | "start_failed" | None
    seconds: float
    results: dict[str, Any]  # parsed results.json: untrusted data
    log: str
    out_dir: Path
    sandbox_id: str | None = None
    destroyed: bool = False
    events: list[str] = field(default_factory=list)


class Wsb:
    """Thin wrapper over the `wsb` CLI. `run` is injectable for tests."""

    def __init__(self, exe: str | None = None, run: Run | None = None) -> None:
        self.exe = exe or shutil.which("wsb")
        self._run = run or subprocess.run
        if not self.exe:
            raise SandboxUnavailable("Windows Sandbox CLI (wsb) was not found.")

    def _call(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
        return self._run(
            [self.exe, *args], capture_output=True, text=True, timeout=timeout, check=False
        )

    def start(self, config: str) -> str:
        proc = self._call("start", "--raw", "-c", config, timeout=120)
        try:
            return str(json.loads(proc.stdout)["Id"])
        except (ValueError, KeyError, TypeError) as exc:
            raise SandboxUnavailable(
                f"wsb start failed: {(proc.stdout + proc.stderr)[:300]}"
            ) from exc

    def exec(
        self, sandbox_id: str, command: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return self._call(
            "exec", "--id", sandbox_id, "-r", "System", "-c", command, timeout=timeout
        )

    def stop(self, sandbox_id: str) -> None:
        self._call("stop", "--id", sandbox_id, timeout=60)

    def running(self) -> set[str]:
        proc = self._call("list", "--raw", timeout=30)
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            return set()
        items = data.get("WindowsSandboxEnvironments", []) if isinstance(data, dict) else data
        if not isinstance(items, list):
            return set()
        return {str(i.get("Id", "") if isinstance(i, dict) else i).lower() for i in items}


def build_config(python_dir: Path, tool_dir: Path, out_dir: Path) -> str:
    """The `.wsb` configuration: no network, three mapped folders, nothing else."""

    def folder(host: Path, guest: str, read_only: bool) -> str:
        return (
            f"<MappedFolder><HostFolder>{escape(str(host))}</HostFolder>"
            f"<SandboxFolder>{escape(guest)}</SandboxFolder>"
            f"<ReadOnly>{'true' if read_only else 'false'}</ReadOnly></MappedFolder>"
        )

    return (
        "<Configuration><Networking>Disable</Networking><vGPU>Disable</vGPU>"
        "<ClipboardRedirection>Disable</ClipboardRedirection><AudioInput>Disable</AudioInput>"
        "<VideoInput>Disable</VideoInput><PrinterRedirection>Disable</PrinterRedirection>"
        "<ProtectedClient>Enable</ProtectedClient>"
        f"<MemoryInMB>{MEMORY_MB}</MemoryInMB><MappedFolders>"
        + folder(python_dir, GUEST_PYTHON, True)
        + folder(tool_dir, GUEST_TOOL, True)
        + folder(out_dir, GUEST_OUT, False)
        + "</MappedFolders></Configuration>"
    )


def _is_link(path: Path) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return True
    return path.is_symlink() or bool(
        getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def stage_tool_folder(tool_dir: Path, staging: Path) -> None:
    """Copy the tool's folder into `staging` (so a later change to the original cannot reach
    the VM). Refuses links, junctions and oversized folders."""
    if not tool_dir.is_dir() or _is_link(tool_dir):
        raise ToolFolderRefused("The tool folder does not exist or is a link.")
    files, total = 0, 0
    for root, dirs, names in os.walk(tool_dir):
        for name in [*dirs, *names]:
            if _is_link(Path(root) / name):
                raise ToolFolderRefused(f"The tool folder contains a link: {name}")
        for name in names:
            files += 1
            total += (Path(root) / name).stat().st_size
        if files > MAX_TOOL_FILES or total > MAX_TOOL_BYTES:
            raise ToolFolderRefused("The tool folder is too large to test in the sandbox.")
    shutil.copytree(tool_dir, staging, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    shutil.copy2(RUNNER_SOURCE, staging / RUNNER_NAME)


@contextmanager
def model_released(backend: Any | None = None, queue: Any | None = None) -> Iterator[bool]:
    """Unload Qwen and hold the model queue for the block; reload afterwards.

    Yields True when the model was loaded before (and will be reloaded). Raises
    SandboxUnavailable, without yielding, if the model cannot be confirmed unloaded.
    """
    if backend is None:
        from model.serving import get_backend

        backend = get_backend()
    if queue is None:
        from agents.runtime.model_queue import get_model_queue

        queue = get_model_queue()
    try:
        was_loaded = bool(backend.runtime_info().get("loaded"))
    except Exception:  # runtime not running: nothing in RAM
        was_loaded = False
    with queue.exclusive():
        if was_loaded:
            backend.unload()
            deadline = time.monotonic() + 30
            while backend.runtime_info().get("loaded"):
                if time.monotonic() > deadline:
                    raise SandboxUnavailable("The model did not unload; sandbox not started.")
                time.sleep(0.5)
        try:
            yield was_loaded
        finally:
            if was_loaded:
                backend.load()


class WindowsSandbox:
    def __init__(
        self,
        wsb: Wsb | None = None,
        python_dir: Path = DEFAULT_PYTHON_DIR,
        runs_dir: Path = RUNS_DIR,
        model_guard: Callable[[], Any] = model_released,
    ) -> None:
        self.wsb = wsb or Wsb()
        self.python_dir = python_dir
        self.runs_dir = runs_dir
        self.model_guard = model_guard

    # -- helpers ---------------------------------------------------------------------------
    @property
    def _registry(self) -> Path:
        return self.runs_dir / REGISTRY_NAME

    def _register(self, sandbox_id: str | None) -> None:
        if sandbox_id:
            self._registry.write_text(json.dumps({"id": sandbox_id}), encoding="utf-8")
        else:
            self._remove_registry(self._registry)

    def _remove_registry(self, path: Path) -> None:
        """The only permanent delete in this module (owner exception, docs/DECISIONS.md):
        the sandbox's own `active.json` in its fixed state folder, nothing else."""
        expected = (self.runs_dir / REGISTRY_NAME).resolve()
        if path.name != REGISTRY_NAME or path.resolve() != expected or _is_link(path):
            raise ValueError("Refusing to delete anything but the sandbox's own active.json.")
        path.unlink(missing_ok=True)

    def cleanup_orphans(self) -> str | None:
        """Stop a VM that an earlier crashed run left behind."""
        try:
            sid = json.loads(self._registry.read_text(encoding="utf-8")).get("id")
        except (OSError, ValueError, AttributeError):
            return None
        if sid:
            self.wsb.stop(sid)
        self._remove_registry(self._registry)
        return sid

    def _destroy(self, sandbox_id: str) -> bool:
        for _ in range(3):
            with contextlib.suppress(Exception):  # keep trying; never leave the VM running
                self.wsb.stop(sandbox_id)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if sandbox_id.lower() not in self.wsb.running():
                    return True
                time.sleep(1)
        return False

    def _wait_ready(self, sandbox_id: str, boot_timeout: float) -> bool:
        deadline = time.monotonic() + boot_timeout
        while time.monotonic() < deadline:
            proc = self.wsb.exec(sandbox_id, "cmd /c exit 0", timeout=30)
            if proc.returncode == 0:
                return True
            time.sleep(3)
        return False

    # -- the one public operation -----------------------------------------------------------
    def run_tool_tests(
        self, tool_dir: Path, *, timeout_s: float = 120, boot_timeout_s: float = 90
    ) -> SandboxRunResult:
        """Run the tool folder's `test*.py` (unittest) inside a fresh Windows Sandbox."""
        if not (self.python_dir / "python.exe").is_file():
            raise SandboxUnavailable(f"Portable Python not found in {self.python_dir}.")
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        run_dir = self.runs_dir / f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
        staging, out_dir = run_dir / "tool", run_dir / "out"
        out_dir.mkdir(parents=True)
        stage_tool_folder(tool_dir.resolve(), staging)
        events: list[str] = []
        started = time.monotonic()
        sandbox_id: str | None = None
        stopped: str | None = None
        log = ""
        with self.model_guard() as model_was_loaded:
            events.append(
                f"model was loaded (unloaded for the run, reloaded after): {model_was_loaded}"
            )
            orphan = self.cleanup_orphans()
            if orphan:
                events.append(f"stopped orphan sandbox {orphan}")
            try:
                sandbox_id = self.wsb.start(build_config(self.python_dir, staging, out_dir))
                self._register(sandbox_id)
                events.append("sandbox started")
                if not self._wait_ready(sandbox_id, boot_timeout_s):
                    stopped = "start_failed"
                else:
                    cmd = (
                        f"cmd /c {GUEST_PYTHON}\\python.exe -I -B {GUEST_TOOL}\\{RUNNER_NAME} "
                        f"{GUEST_TOOL} {GUEST_OUT} > {GUEST_OUT}\\runner.log 2>&1"
                    )
                    try:
                        self.wsb.exec(sandbox_id, cmd, timeout=timeout_s)
                    except subprocess.TimeoutExpired:
                        stopped = "timeout"
            finally:
                destroyed = self._destroy(sandbox_id) if sandbox_id else True
                if destroyed:
                    self._register(None)
                events.append(f"sandbox destroyed: {destroyed}")
        results: dict[str, Any] = {}
        result_file = out_dir / "results.json"
        if result_file.is_file() and result_file.stat().st_size <= MAX_RESULT_BYTES:
            try:
                parsed = json.loads(result_file.read_text(encoding="utf-8"))
                results = parsed if isinstance(parsed, dict) else {}
            except (OSError, ValueError):
                results = {}
        log_file = out_dir / "runner.log"
        if log_file.is_file():
            log = log_file.read_text(encoding="utf-8", errors="replace")[:MAX_RESULT_BYTES]
        return SandboxRunResult(
            passed=bool(results.get("passed")) and stopped is None,
            stopped=stopped,
            seconds=round(time.monotonic() - started, 1),
            results=results,
            log=log,
            out_dir=out_dir,
            sandbox_id=sandbox_id,
            destroyed=destroyed,
            events=events,
        )
