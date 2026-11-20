"""Push-to-talk microphone capture, in memory only.

The microphone is open only between `start()` and `stop()` (the owner holding the talk
button or hotkey), and never longer than `max_seconds`. Samples are kept in a Python list
and handed back as one array; nothing is written to disk here.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

import numpy as np

from voice.stt.whisper_stt import SAMPLE_RATE


class InputStream(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


StreamFactory = Callable[[Callable[..., None]], InputStream]

# One set of capture settings for the app AND the benchmark recorder
# (voice/bench/record_samples.py), so app audio matches the clips Whisper was scored on:
# the default input device, 16 kHz, mono, 16-bit integers, no gain applied.
CAPTURE: dict[str, Any] = {"samplerate": SAMPLE_RATE, "channels": 1, "dtype": "int16"}


def input_device_name() -> str:
    import sounddevice as sd

    try:
        return str(sd.query_devices(kind="input")["name"])
    except Exception:
        return "no microphone found"


def sounddevice_stream(callback: Callable[..., None]) -> InputStream:
    import sounddevice as sd

    stream: InputStream = sd.InputStream(callback=callback, **CAPTURE)
    return stream


def to_float(data: Any) -> np.ndarray:
    """int16 samples (as captured) -> float32 in [-1, 1], the way Whisper reads them."""
    arr = np.asarray(data)
    if arr.dtype == np.int16:
        return arr.reshape(-1).astype(np.float32) / 32768.0
    return arr.reshape(-1).astype(np.float32)


class PushToTalkRecorder:
    def __init__(
        self, max_seconds: float, stream_factory: StreamFactory = sounddevice_stream
    ) -> None:
        self.max_samples = int(max_seconds * SAMPLE_RATE)
        self._factory = stream_factory
        self._chunks: list[np.ndarray] = []
        self._count = 0
        self._stream: InputStream | None = None
        self._lock = threading.Lock()
        self.started_at = 0.0

    @property
    def recording(self) -> bool:
        return self._stream is not None

    @property
    def full(self) -> bool:
        return self._count >= self.max_samples

    def _on_audio(self, data: Any, _frames: int, _time: Any, _status: Any) -> None:
        with self._lock:
            room = self.max_samples - self._count
            if room <= 0:
                return
            chunk = to_float(data)[:room].copy()
            self._chunks.append(chunk)
            self._count += len(chunk)

    def start(self) -> None:
        if self._stream is not None:
            return
        with self._lock:
            self._chunks, self._count = [], 0
        stream = self._factory(self._on_audio)
        stream.start()
        self._stream = stream
        self.started_at = time.monotonic()

    def close(self) -> None:
        """Close the microphone; what it heard stays in memory until `take()` or `stop()`."""
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.stop()
            stream.close()

    def stop(self) -> np.ndarray:
        """Close the microphone and return what it heard (float32, 16 kHz mono)."""
        self.close()
        with self._lock:
            audio = np.concatenate(self._chunks) if self._chunks else np.zeros(0, np.float32)
            self._chunks, self._count = [], 0
        return audio
