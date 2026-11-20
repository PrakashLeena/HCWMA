"""Start a program hidden and detached from HCWMA (Windows). Used only to start the owner's
installed Ollama server (`ollama.exe serve`) when it is not running; see
model/serving/ollama_launcher.py. No agent or tool can reach this: the model never executes
anything (architecture rule 1).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol

DETACHED_PROCESS = 0x00000008  # no console window; keeps running when HCWMA exits
CREATE_NEW_PROCESS_GROUP = 0x00000200  # Ctrl+C in HCWMA's console does not reach it
SW_HIDE = 0


class Started(Protocol):
    pid: int

    def poll(self) -> int | None: ...


def start_hidden_detached(argv: list[str], env: dict[str, str], cwd: Path) -> Started:
    """Start `argv` with no window, detached, output discarded. Returns the process handle."""
    info = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
    info.wShowWindow = SW_HIDE
    return subprocess.Popen(  # noqa: S603 - fixed argv: the installed ollama.exe and "serve"
        argv,
        env=env,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        startupinfo=info,
    )
