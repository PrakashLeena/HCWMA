"""Runs agent-written code in the sandbox (build step 11; master document 6.1, 9.3, 9.4).

Only two command shapes can be started, never through a shell:

    python <script.py> [args...]
    python -m pytest|unittest [args...]

Each run is a fresh child process (`python -I -B`) started inside `workspace/code/` with a
scrubbed environment (no API keys, no user profile, temp folder inside the sandbox), the
in-process guard from `sandbox_guard.py`, a wall-clock timeout, a cap on output size and on
memory, and the whole process tree is killed when a limit is hit. The output goes to a log
file in `code/.hcwma/logs/` so `read_logs` can show it again.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psutil

from integrations.adapters.pc.sandbox_guard import ALLOWED_MODULES, ROOT_ENV

GUARD_PATH = Path(__file__).resolve().with_name("sandbox_guard.py")
INTERNAL_DIR = ".hcwma"  # inside the code root: temp files and run logs
MAX_LOG_BYTES = 2_000_000
MAX_MEMORY_BYTES = 768 * 1024 * 1024
KEEP_LOGS = 30
PYTHON_NAMES = {"python", "python3", "py"}

_ENV_KEEP = ("SYSTEMROOT", "SystemRoot", "COMSPEC", "PATHEXT", "LANG", "LC_ALL")


class CommandRefused(ValueError):
    """The command is not one of the allowed shapes. The message is safe to show the agent."""


@dataclass(frozen=True)
class RunResult:
    exit_code: int | None  # None when a limit stopped it
    stopped: str | None  # "timeout" | "output_limit" | "memory_limit" | None
    seconds: float
    output: str  # the whole log, up to MAX_LOG_BYTES
    log_path: Path


def internal_dir(code_root: Path) -> Path:
    return code_root / INTERNAL_DIR


def parse_command(command: str) -> tuple[str, list[str]]:
    """`("script", [file, args...])` or `("module", [name, args...])`, or CommandRefused."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError as exc:
        raise CommandRefused(f"cannot read the command: {exc}") from exc
    if not tokens or tokens[0].lower().removesuffix(".exe") not in PYTHON_NAMES:
        raise CommandRefused(
            "only 'python <script.py> [args]' or 'python -m pytest [args]' can be run here"
        )
    rest = tokens[1:]
    if not rest:
        raise CommandRefused("say which script to run, for example: python main.py")
    if rest[0] == "-m":
        if len(rest) < 2 or rest[1] not in ALLOWED_MODULES:
            raise CommandRefused(f"only 'python -m {' or '.join(ALLOWED_MODULES)}' is allowed")
        return "module", rest[1:]
    if rest[0].startswith("-"):
        raise CommandRefused("python options such as -c are not allowed; run a script file")
    if not rest[0].lower().endswith(".py"):
        raise CommandRefused("the script must be a .py file")
    return "script", rest


def _scrubbed_env(code_root: Path) -> dict[str, str]:
    tmp = internal_dir(code_root) / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
    env["PATH"] = os.pathsep.join({str(Path(sys.executable).parent), str(Path(sys.base_prefix))})
    env.update(
        {
            ROOT_ENV: str(code_root),
            "TEMP": str(tmp),
            "TMP": str(tmp),
            "TMPDIR": str(tmp),
            "HOME": str(code_root),
            "USERPROFILE": str(code_root),
            "NO_COLOR": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    try:
        parent = psutil.Process(proc.pid)
        victims = [*parent.children(recursive=True), parent]
    except psutil.NoSuchProcess:
        return
    for p in victims:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
    proc.wait(timeout=5)


def _rss(proc: subprocess.Popen[bytes]) -> int:
    try:
        return psutil.Process(proc.pid).memory_info().rss
    except psutil.Error:
        return 0


def _new_log(code_root: Path, kind: str) -> Path:
    logs = internal_dir(code_root) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    old = sorted(logs.glob("*.log"))
    for stale in old[: max(0, len(old) - KEEP_LOGS + 1)]:
        stale.unlink(missing_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return logs / f"{stamp}-{kind}.log"


def run(
    command: str,
    *,
    code_root: Path,
    cwd: Path,
    timeout_s: float,
    kind: str = "run",
    extra_args: list[str] | None = None,
) -> RunResult:
    mode, rest = parse_command(command)
    if extra_args:
        rest = [*rest, *extra_args]
    if not cwd.is_dir():
        raise CommandRefused(f"the folder '{cwd}' does not exist")
    if mode == "script":
        script = (cwd / rest[0]).resolve()
        if not script.is_file() or not _is_inside(script, code_root):
            raise CommandRefused(f"there is no script '{rest[0]}' in the working folder")
        rest = [str(script), *rest[1:]]
    argv = [sys.executable, "-I", "-B", "-X", "utf8", str(GUARD_PATH), mode, *rest]

    log_path = _new_log(code_root, kind)
    started = time.monotonic()
    stopped: str | None = None
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    with log_path.open("wb") as log:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell, scrubbed env
            argv,
            cwd=cwd,
            env=_scrubbed_env(code_root),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        while proc.poll() is None:
            if time.monotonic() - started > timeout_s:
                stopped = "timeout"
            elif log_path.stat().st_size > MAX_LOG_BYTES:
                stopped = "output_limit"
            elif _rss(proc) > MAX_MEMORY_BYTES:
                stopped = "memory_limit"
            if stopped:
                _kill_tree(proc)
                break
            time.sleep(0.05)
    output = log_path.read_bytes()[:MAX_LOG_BYTES].decode("utf-8", errors="replace")
    return RunResult(
        exit_code=None if stopped else proc.returncode,
        stopped=stopped,
        seconds=round(time.monotonic() - started, 2),
        output=output,
        log_path=log_path,
    )


def _is_inside(path: Path, folder: Path) -> bool:
    return Path(os.path.normcase(str(path))).is_relative_to(
        Path(os.path.normcase(str(folder.resolve())))
    )
