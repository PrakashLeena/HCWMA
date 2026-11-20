"""Speech-to-text with faster-whisper on the CPU (int8), loaded only while it is needed.

The model is loaded when the owner starts talking (in the background, while the microphone
records) and unloaded right after the transcription, so Whisper and Qwen are never both
holding RAM while HCWMA is idle (owner requirement, step 14).
"""

from __future__ import annotations

import gc
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

SAMPLE_RATE = 16_000


class SpeechToText:
    """One faster-whisper model, loaded on demand and dropped after use."""

    def __init__(self, model_name: str, models_dir: Path, max_seconds: float) -> None:
        self.model_name = model_name
        self.model_dir = models_dir / model_name
        self.max_seconds = max_seconds
        self._model: Any = None
        self._error: str | None = None
        self._lock = threading.Lock()
        self._loader: threading.Thread | None = None

    def available(self) -> bool:
        return (self.model_dir / "model.bin").exists()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load_in_background(self, before: Callable[[], None] | None = None) -> None:
        """Load in a thread. `before` runs first in that thread (e.g. wait for Qwen to unload)."""
        with self._lock:
            if self._model is not None or (self._loader and self._loader.is_alive()):
                return
            self._error = None
            self._loader = threading.Thread(
                target=self._load, args=(before,), name="whisper-load", daemon=True
            )
            self._loader.start()

    def _load(self, before: Callable[[], None] | None = None) -> None:
        try:
            if before is not None:
                before()
            from faster_whisper import WhisperModel

            model = WhisperModel(str(self.model_dir), device="cpu", compute_type="int8")
            with self._lock:
                self._model = model
        except Exception as exc:  # reported to the owner when they stop talking
            self._error = type(exc).__name__

    def transcribe(self, audio: np.ndarray, language: str) -> str:
        """Text heard in `audio` (float32, 16 kHz mono). Waits for the model to finish loading."""
        if self._loader is not None:
            self._loader.join()
        if self._model is None:
            raise RuntimeError(f"the speech model could not be loaded ({self._error or 'missing'})")
        segments, _ = self._model.transcribe(
            audio,
            language=language,
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
            without_timestamps=True,
            max_new_tokens=96,  # a hallucination loop cannot run on (seen in the benchmark)
        )
        deadline = time.monotonic() + self.max_seconds
        parts: list[str] = []
        for seg in segments:
            parts.append(seg.text.strip())
            if time.monotonic() > deadline:
                break
        return " ".join(p for p in parts if p).strip()

    def unload(self) -> None:
        if self._loader is not None:
            self._loader.join()
        with self._lock:
            self._model = None
            self._loader = None
        gc.collect()
