"""Start the owner's installed Ollama when HCWMA needs the model and it is not running.

Owner decision 2026-11-20 (docs/DECISIONS.md):
- "Down" means nothing answers on the configured endpoint (127.0.0.1:11434).
- The installed `ollama.exe serve` is started hidden and detached, with OLLAMA_HOST set to
  127.0.0.1:<port> (never 0.0.0.0, architecture rule 8) and OLLAMA_MODELS set from
  `ollama_models_dir` in configs/model.yaml.
- Backlog item 1 (2026-11-21): it also gets OLLAMA_NUM_PARALLEL=1, OLLAMA_MAX_LOADED_MODELS=1
  and llama-server's prompt-cache / context-checkpoint limits, so its RAM stays bounded.
- ollama.exe is looked up on PATH, then in the standard install folders. Nothing is ever
  downloaded or installed.
- One start attempt at a time (a lock); other requests wait for that attempt. Health is
  polled for up to `ollama_start_timeout_s` (60 s). A failed attempt is not repeated for the
  next 60 s: every waiting request gets the same clear message instead of a loop.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from model.serving.config import ModelConfig, load_model_config

State = Literal["unknown", "up", "starting", "failed"]

WHAT_TO_DO = (
    "Start Ollama from the Start menu (or run  ollama serve  in a terminal), then ask again."
)


class LaunchResult(BaseModel):
    ok: bool
    started: bool = False  # True when HCWMA started Ollama for this request
    seconds: float = 0.0
    reason: str = ""

    def message(self) -> str:
        if self.ok:
            return ""
        return f"The local model could not be started: {self.reason} {WHAT_TO_DO}"


def standard_install_paths() -> list[Path]:
    """Where the Ollama installer puts ollama.exe (per-user first, then machine-wide)."""
    paths: list[Path] = []
    for var, sub in (
        ("LOCALAPPDATA", "Programs/Ollama/ollama.exe"),
        ("ProgramFiles", "Ollama/ollama.exe"),
    ):
        base = os.environ.get(var)
        if base:
            paths.append(Path(base) / sub)
    return paths


def find_ollama(
    which: Callable[[str], str | None] = shutil.which,
    candidates: Callable[[], list[Path]] = standard_install_paths,
) -> Path | None:
    found = which("ollama")
    if found:
        return Path(found)
    return next((p for p in candidates() if p.is_file()), None)


def http_probe(base_url: str) -> bool:
    """True when Ollama answers. Connection refused (nothing listening) means down."""
    try:
        with urllib.request.urlopen(base_url + "/api/version", timeout=2) as resp:  # noqa: S310 - 127.0.0.1 only (ModelConfig)
            return bool(resp.status == 200)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _default_starter(argv: list[str], env: dict[str, str], cwd: Path) -> Any:
    from integrations.adapters.pc.windows_process import start_hidden_detached

    return start_hidden_detached(argv, env, cwd)


class OllamaLauncher:
    def __init__(
        self,
        config: ModelConfig | None = None,
        *,
        probe: Callable[[], bool] | None = None,
        finder: Callable[[], Path | None] = find_ollama,
        starter: Callable[[list[str], dict[str, str], Path], Any] = _default_starter,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        poll_s: float = 1.0,
    ) -> None:
        self.config = config or load_model_config()
        base = self.config.endpoint.base_url
        self._probe = probe or (lambda: http_probe(base))
        self._finder = finder
        self._starter = starter
        self._clock = clock
        self._sleep = sleep
        self.poll_s = poll_s
        self._lock = threading.Lock()
        self.state: State = "unknown"
        self.last: LaunchResult | None = None
        self._failed_at: float | None = None
        self.started_by_hcwma = False

    @property
    def timeout_s(self) -> float:
        return self.config.ollama_start_timeout_s

    def is_up(self) -> bool:
        up = self._probe()
        if up:
            self.state = "up"
        return up

    def environment(self) -> dict[str, str]:
        env = dict(os.environ)
        env["OLLAMA_HOST"] = f"{self.config.endpoint.host}:{self.config.endpoint.port}"
        if self.config.ollama_models_dir:
            env["OLLAMA_MODELS"] = self.config.ollama_models_dir
        # Backlog item 1: bounded RAM. Ollama passes LLAMA_ARG_* on to llama-server.
        env["OLLAMA_NUM_PARALLEL"] = str(self.config.ollama_num_parallel)
        env["OLLAMA_MAX_LOADED_MODELS"] = "1"
        env["LLAMA_ARG_CACHE_RAM"] = str(self.config.prompt_cache_mb)
        env["LLAMA_ARG_CTX_CHECKPOINTS"] = str(self.config.context_checkpoints)
        return env

    def ensure_running(self) -> LaunchResult:
        """Return at once when Ollama answers; otherwise start it (once) and wait for it."""
        if self.is_up():
            return LaunchResult(ok=True)
        with self._lock:  # one start attempt at a time; others wait here for its result
            if self.is_up():
                return LaunchResult(ok=True)
            now = self._clock()
            recent = self._failed_at is not None and now - self._failed_at < self.timeout_s
            if recent and self.last is not None:
                return self.last  # just failed: do not try again yet
            result = self._start_and_wait()
            self.last = result
            self._failed_at = None if result.ok else self._clock()
            self.state = "up" if result.ok else "failed"
            return result

    def _start_and_wait(self) -> LaunchResult:
        self.state = "starting"
        t0 = self._clock()
        exe = self._finder()
        if exe is None:
            return LaunchResult(
                ok=False,
                reason=(
                    "Ollama is not installed where HCWMA looks (PATH, "
                    "%LOCALAPPDATA%\\Programs\\Ollama, %ProgramFiles%\\Ollama). "
                    "HCWMA does not download or install it."
                ),
            )
        try:
            proc = self._starter([str(exe), "serve"], self.environment(), exe.parent)
        except OSError as exc:
            return LaunchResult(ok=False, reason=f"{exe} could not be run ({type(exc).__name__}).")
        self.started_by_hcwma = True
        deadline = t0 + self.timeout_s
        while self._clock() < deadline:
            if self._probe():
                return LaunchResult(ok=True, started=True, seconds=round(self._clock() - t0, 1))
            code = proc.poll() if proc is not None else None
            if code is not None:
                return LaunchResult(
                    ok=False,
                    started=True,
                    seconds=round(self._clock() - t0, 1),
                    reason=f"ollama serve stopped right away (exit code {code}).",
                )
            self._sleep(self.poll_s)
        return LaunchResult(
            ok=False,
            started=True,
            seconds=round(self._clock() - t0, 1),
            reason=f"Ollama was started but did not answer within {self.timeout_s:.0f} s.",
        )

    def health_value(self) -> str:
        """One line for the Health page."""
        if self.state == "starting":
            return "starting (HCWMA started it)…"
        if self.state == "failed" and self.last is not None:
            return f"not running: {self.last.reason}"
        if self.started_by_hcwma and self.state == "up":
            return "started by HCWMA"
        return ""


@lru_cache(maxsize=1)
def get_launcher() -> OllamaLauncher:
    """The single launcher for this process (one lock for every request)."""
    return OllamaLauncher()
