"""RAM used by Ollama's model process (backlog item 1).

Ollama runs the model in a child process: `llama-server.exe` in current versions,
`ollama runner` / `ollama_llama_server` in older ones. Only `model/serving/` knows that.
"""

from __future__ import annotations

from typing import NamedTuple

import psutil

MB = 1024 * 1024
RUNNER_NAMES = ("llama-server", "ollama_llama_server")


class RunnerMemory(NamedTuple):
    working_set_mb: int  # RAM the process occupies now
    private_mb: int  # memory committed to the process alone (includes paged-out parts)
    processes: int


def _is_runner(name: str, cmdline: list[str]) -> bool:
    name = name.lower()
    if any(n in name for n in RUNNER_NAMES):
        return True
    return name.startswith("ollama") and "runner" in cmdline


def runner_memory() -> RunnerMemory:
    """Summed over every model runner process; zeros when no model is loaded."""
    ws = priv = count = 0
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if not _is_runner(proc.info.get("name") or "", proc.info.get("cmdline") or []):
                continue
            info = proc.memory_info()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        ws += info.rss
        priv += int(getattr(info, "private", 0) or info.vms)
        count += 1
    return RunnerMemory(ws // MB, priv // MB, count)
