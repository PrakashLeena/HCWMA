"""Runs one approved generated tool in its own process (build step 13).

Why a process: a tool that loops forever, eats memory or crashes must not take the tool
server down, and the process can be killed. The child is `runner.py` under the in-process
guard of `sandbox_guard.py` (no file access outside an empty folder, no network, no new
processes) with a scrubbed environment. Web access is not done by the child: it asks this
module, which allows only the hosts in the manifest and uses the shared fetch with its
private-address checks (`fetch_public(..., allowed_hosts=...)`).

Nothing here deletes anything: the child's folder is one fixed empty folder that is reused.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil

from integrations.adapters.browser.web_tools import FetchedPage, ToolRefused, fetch_public
from integrations.adapters.pc.sandbox_guard import ROOT_ENV
from integrations.adapters.pc.sandbox_runner import GUARD_PATH
from integrations.generated_tools.store import STATE_DIR
from memory.config import resolve_data_dir

RUNNER_SOURCE = Path(__file__).resolve().with_name("runner.py")
CHECK_SOURCE = Path(__file__).resolve().with_name("static_check.py")  # the runner imports it
MAX_MEMORY_BYTES = 512 * 1024 * 1024
MAX_LINE_BYTES = 2_000_000
MAX_FETCH_TEXT = 100_000

HostFetcher = Callable[[str, frozenset[str]], FetchedPage]


class GeneratedToolError(RuntimeError):
    """The tool failed or was stopped. The message is short and safe to show an agent."""


def _scrubbed_env(root: Path) -> dict[str, str]:
    keep = ("SYSTEMROOT", "SystemRoot", "COMSPEC", "PATHEXT", "LANG", "LC_ALL")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PATH"] = os.pathsep.join({str(Path(sys.executable).parent), str(Path(sys.base_prefix))})
    env.update(
        {
            ROOT_ENV: str(root),
            "TEMP": str(root),
            "TMP": str(root),
            "TMPDIR": str(root),
            "HOME": str(root),
            "USERPROFILE": str(root),
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


def _run_root() -> Path:
    root = resolve_data_dir() / STATE_DIR / "run"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _kill_tree(proc: subprocess.Popen[str]) -> None:
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
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _rss(proc: subprocess.Popen[str]) -> int:
    try:
        return psutil.Process(proc.pid).memory_info().rss
    except psutil.Error:
        return 0


def _host_fetch(url: str, hosts: frozenset[str]) -> FetchedPage:
    return fetch_public(url, allowed_hosts=hosts)


def run_generated(
    files: dict[str, bytes],
    approved_hash: str,
    args: dict[str, Any],
    *,
    hosts: frozenset[str] = frozenset(),
    timeout_s: float = 10.0,
    fetch: HostFetcher = _host_fetch,
) -> dict[str, Any]:
    """Run the tool's `run(args, ctx)` and return its dict. Raises `GeneratedToolError`.

    `files` are the tool's exact bytes and `approved_hash` the hash the owner approved. The
    child recomputes the hash and re-runs the static check immediately before `exec`."""
    root = _run_root()
    for source_file in (RUNNER_SOURCE, CHECK_SOURCE):
        target = root / source_file.name
        text = source_file.read_text(encoding="utf-8")
        if not target.is_file() or target.read_text(encoding="utf-8") != text:
            target.write_text(text, encoding="utf-8")
    runner = root / RUNNER_SOURCE.name
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    argv = [sys.executable, "-I", "-B", "-X", "utf8", str(GUARD_PATH), "script", str(runner)]
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell, scrubbed env
        argv,
        cwd=root,
        env=_scrubbed_env(root),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        bufsize=1,
        creationflags=flags,
    )
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        for line in proc.stdout or ():
            lines.put(line[:MAX_LINE_BYTES])
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()

    def send(obj: dict[str, Any]) -> None:
        if proc.stdin is None:
            raise BrokenPipeError
        proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    started = time.monotonic()
    try:
        send(
            {
                "files": {n: base64.b64encode(d).decode("ascii") for n, d in files.items()},
                "approved_hash": approved_hash,
                "args": args,
            }
        )
        while True:
            if time.monotonic() - started > timeout_s:
                raise GeneratedToolError(f"the tool did not finish within {timeout_s:g} s")
            if _rss(proc) > MAX_MEMORY_BYTES:
                raise GeneratedToolError("the tool used too much memory")
            try:
                line = lines.get(timeout=0.05)
            except queue.Empty:
                continue
            if line is None:
                raise GeneratedToolError("the tool stopped without an answer")
            try:
                message = json.loads(line)
            except ValueError:
                continue  # not protocol: ignore
            if not isinstance(message, dict):
                continue
            if "fetch" in message:
                send(_answer_fetch(str(message["fetch"]), hosts, fetch))
            elif "result" in message and isinstance(message["result"], dict):
                return message["result"]
            elif "error" in message:
                raise GeneratedToolError(str(message["error"])[:300])
    except (BrokenPipeError, OSError) as exc:
        raise GeneratedToolError(f"the tool process failed ({type(exc).__name__})") from exc
    finally:
        _kill_tree(proc)


def _answer_fetch(url: str, hosts: frozenset[str], fetch: HostFetcher) -> dict[str, Any]:
    if not hosts:
        return {"ok": False, "error": "this tool declared no network hosts"}
    try:
        page = fetch(url, hosts)
    except ToolRefused as exc:
        return {"ok": False, "error": str(exc)[:200]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"the request failed ({type(exc).__name__})"}
    return {
        "ok": True,
        "status": page.status,
        "final_url": page.final_url,
        "text": page.body[:MAX_FETCH_TEXT],
    }
