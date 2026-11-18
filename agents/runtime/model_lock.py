"""A lock both HCWMA processes honour while Windows Sandbox is running a tool test.

The sandbox run unloads Qwen to free RAM (`integrations/adapters/pc/windows_sandbox.py`). That
happens in the tool-server process, which cannot hold the backend's in-process model queue.
So the run writes this small file in the HCWMA data folder, and the backend's `ModelQueue`
refuses to start inference while it says "active", so nothing reloads the model mid-run.

The file is never deleted (architecture rule 9): releasing writes `active: false`. A lock
also stops counting when its time is up or its owner process no longer exists, so a crash
cannot leave the model blocked.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psutil

from memory.config import resolve_data_dir

LOCK_NAME = "model_busy.json"
DEFAULT_TTL_S = 420.0  # longer than the longest sandbox run (boot + tests + reload)
BUSY_MESSAGE = "busy: testing a tool, try again shortly"


class ModelBusy(RuntimeError):
    """The model is being kept free for a sandbox test. The message is safe to show."""

    def __init__(self) -> None:
        super().__init__(BUSY_MESSAGE)


def lock_path() -> Path:
    return resolve_data_dir() / LOCK_NAME


def _write(payload: dict[str, object]) -> None:
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    Path(tmp).replace(path)  # atomic: a reader sees the old or the new file, never half


def is_busy(now: float | None = None) -> bool:
    try:
        data = json.loads(lock_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or data.get("active") is not True:
        return False
    if (now if now is not None else time.time()) >= float(data.get("until", 0)):
        return False
    pid = data.get("pid")
    return isinstance(pid, int) and _alive(pid)


def _alive(pid: int) -> bool:
    try:
        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def check_free() -> None:
    """Raise `ModelBusy` while a sandbox run holds the model."""
    if is_busy():
        raise ModelBusy


@contextmanager
def hold(reason: str = "sandbox", ttl_s: float = DEFAULT_TTL_S) -> Iterator[None]:
    """Mark the model busy for the block. Written first, released last."""
    _write({"active": True, "pid": os.getpid(), "until": time.time() + ttl_s, "reason": reason})
    try:
        yield
    finally:
        _write({"active": False})
