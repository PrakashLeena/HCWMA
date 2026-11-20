"""A one-line "what HCWMA is doing right now" notice for the chat page (e.g. "Opening WhatsApp…").

Tools set it from code while a slow step runs; the chat page shows it in place of "HCWMA is
working…" (it asks `/model/state` every second while a request waits). Tools run in the MCP
server's own process, so the notice is a tiny file in the HCWMA data folder that the backend
reads. Only fixed texts written by HCWMA's own code are posted here, never tool output or model
text. A notice older than `MAX_AGE_S` is ignored (a crash cannot leave it showing).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

FILE_NAME = "activity.json"
MAX_AGE_S = 120.0


def _path() -> Path:
    from memory.config import resolve_data_dir

    return resolve_data_dir() / FILE_NAME


def current() -> str:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        if time.time() - float(data["at"]) > MAX_AGE_S:
            return ""
        return str(data["text"])[:120]
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def set_notice(text: str) -> None:
    path = _path()
    try:
        if not text and not path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"text": text[:120], "at": time.time()}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # the notice is a nicety: never fail the tool over it


@contextmanager
def notice(text: str) -> Iterator[None]:
    """Show `text` while the block runs, then clear it."""
    set_notice(text)
    try:
        yield
    finally:
        with suppress(Exception):
            set_notice("")
