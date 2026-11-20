"""RAM for push-to-talk: is there room for Whisper, and if not, what can be done.

Order (owner's rules, step 14):
1. Enough free RAM for Whisper small (measured peak + margin): just load it.
2. Not enough, but unloading Qwen would make enough: unload Qwen (same model-busy lock and
   queue hold as the Windows Sandbox run), transcribe, then reload Qwen before the Router call.
3. Still not enough: refuse and name the 5 apps using the most RAM so the owner can close
   some. HCWMA never closes, kills or trims any process itself.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

import psutil

# Measured on this PC (docs/voice_benchmarks_stt.md): whole-process peak with Whisper small
# int8 loaded and transcribing = 1038 MB. Plus a safety margin for the OS and buffers.
WHISPER_SMALL_PEAK_MB = 1038
SAFETY_MARGIN_MB = 300


def free_ram_mb() -> float:
    return psutil.virtual_memory().available / 2**20


def qwen_loaded_mb() -> float:
    """RAM Ollama holds for the loaded model(s), 0 when nothing is loaded or Ollama is down."""
    try:
        from model.serving import get_backend

        loaded = get_backend().runtime_info().get("loaded") or []
        return sum(int(m.get("size", 0) or 0) for m in loaded if isinstance(m, dict)) / 2**20
    except Exception:
        return 0.0


def top_apps_by_ram(n: int = 5) -> list[tuple[str, int]]:
    """(app name, MB) for the n apps using the most RAM, processes of one app added up.
    Read-only: nothing is closed or changed."""
    totals: dict[str, int] = {}
    for proc in psutil.process_iter(["name", "memory_info"]):
        info = proc.info
        mem = info.get("memory_info")
        name = info.get("name") or "?"
        if mem is None:
            continue
        totals[name] = totals.get(name, 0) + int(mem.rss)
    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:n]
    return [(name.removesuffix(".exe"), rss // 2**20) for name, rss in ranked]


def close_apps_message(need_mb: int, free_mb: float, apps: list[tuple[str, int]]) -> str:
    listing = ", ".join(f"{name} {mb} MB" for name, mb in apps)
    return (
        f"Not enough free RAM for speech recognition: it needs about {need_mb} MB free, "
        f"{free_mb:.0f} MB is free now (even with the model unloaded). "
        f"Close some of these to free RAM: {listing}. HCWMA does not close apps itself."
    )


GuardFactory = Callable[[], AbstractContextManager[Any]]


def default_guard() -> AbstractContextManager[Any]:
    from integrations.adapters.pc.windows_sandbox import model_released

    return model_released(reason="voice: speech recognition")


class QwenRelease:
    """Holds Qwen unloaded from `start()` until `finish()`, in its own thread.

    Uses the same guard as a sandbox run: the model-busy file lock plus the model queue held,
    so nothing reloads Qwen mid-way; leaving the guard reloads Qwen.
    """

    def __init__(self, guard: GuardFactory = default_guard) -> None:
        self._guard = guard
        self._ready = threading.Event()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self.error = ""
        self.unload_s = 0.0
        self.reload_s = 0.0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="qwen-release", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        t0 = time.perf_counter()
        try:
            with self._guard():
                self.unload_s = time.perf_counter() - t0
                self._ready.set()
                self._done.wait()
                t1 = time.perf_counter()
            self.reload_s = time.perf_counter() - t1  # leaving the guard reloads Qwen
        except Exception as exc:  # e.g. Qwen did not unload in time
            self.error = type(exc).__name__
        finally:
            self._ready.set()

    def wait_unloaded(self, timeout: float = 60) -> None:
        """Called before Whisper loads. Raises if Qwen could not be unloaded."""
        if not self._ready.wait(timeout) or self.error:
            raise RuntimeError(
                f"could not unload the model to make room ({self.error or 'timeout'})"
            )

    def finish(self, timeout: float = 120) -> None:
        """Let Qwen reload, and wait until it has (so the Router call does not pay for it)."""
        self._done.set()
        if self._thread is not None:
            self._thread.join(timeout)
