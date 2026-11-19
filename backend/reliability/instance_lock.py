"""One HCWMA at a time (step 18).

An old instance left running kept serving old code to a newly started app. Now the desktop
app and the stand-alone backend both take this lock before they start; a second one is
refused and told which process holds it.

The lock is an OS file lock on `<data dir>/app.lock`: the OS releases it when the process
ends for any reason (crash, kill, power loss), so a stale lock can never block a restart.
Who holds it is written next to it in `app_instance.json` (information only).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import IO, Any

import psutil

from memory.config import resolve_data_dir

LOCK_FILE = "app.lock"
INFO_FILE = "app_instance.json"


def _try_lock(handle: IO[str]) -> bool:
    """Lock the first byte without waiting. Stdlib only: msvcrt on Windows, fcntl elsewhere."""
    handle.seek(0)
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle: IO[str]) -> None:
    handle.seek(0)
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


class InstanceLock:
    def __init__(self, data_dir: Path | None = None) -> None:
        base = data_dir or resolve_data_dir()
        self.path = base / LOCK_FILE
        self.info_path = base / INFO_FILE
        self._handle: IO[str] | None = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self, kind: str) -> bool:
        """True if this process now holds the lock. `kind`: "desktop app" or "backend"."""
        if self._handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        if not _try_lock(handle):
            handle.close()
            return False
        self._handle = handle
        info = {"pid": os.getpid(), "kind": kind, "started": time.time()}
        self.info_path.write_text(json.dumps(info), encoding="utf-8")
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        _unlock(self._handle)
        self._handle.close()
        self._handle = None

    def holder(self) -> dict[str, Any] | None:
        """Who holds the lock (pid, kind, started), if that process is still alive."""
        try:
            info = json.loads(self.info_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        pid = info.get("pid") if isinstance(info, dict) else None
        if not isinstance(pid, int) or not psutil.pid_exists(pid):
            return None
        return info

    def refusal(self) -> str:
        holder = self.holder()
        if holder is None:
            return "HCWMA is already running. Use its tray icon (Quit HCWMA) before starting again."
        started = time.strftime("%H:%M", time.localtime(float(holder.get("started", 0))))
        return (
            f"HCWMA is already running ({holder.get('kind', 'app')}, process {holder['pid']}, "
            f"started {started}). Only one may run at a time: use its tray icon, or quit it "
            "(tray: Quit HCWMA) before starting again."
        )
