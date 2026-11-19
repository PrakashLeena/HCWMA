"""Test doubles for the voice service: a fake microphone, Whisper and speaker output."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from voice.config import VoiceConfig
from voice.service import VoiceService
from voice.tts.speech import Speaker


class FakeMic:
    """Feeds `seconds` of a quiet tone to the recorder when the stream starts."""

    def __init__(self, seconds: float = 2.0) -> None:
        self.seconds = seconds
        self.opened = 0
        self.closed = 0
        self.open_now = False

    def factory(self, callback: Callable[..., None]) -> FakeMic:
        self._callback = callback
        return self

    def start(self) -> None:
        self.opened += 1
        self.open_now = True
        n = int(self.seconds * 16_000)
        block = 1600
        tone = (0.1 * np.sin(np.arange(n) / 10)).astype(np.float32).reshape(-1, 1)
        for i in range(0, n, block):
            self._callback(tone[i : i + block], block, None, None)

    def stop(self) -> None:
        self.open_now = False

    def close(self) -> None:
        self.closed += 1


class FakeStt:
    def __init__(self, text: str = "show my downloads", available: bool = True) -> None:
        self.text = text
        self._available = available
        self.loaded = False
        self.loads = 0
        self.unloads = 0
        self.heard: list[tuple[int, str]] = []

    def available(self) -> bool:
        return self._available

    def load_in_background(self) -> None:
        self.loads += 1
        self.loaded = True

    def transcribe(self, audio: np.ndarray, language: str) -> str:
        self.heard.append((audio.size, language))
        return self.text

    def unload(self) -> None:
        self.unloads += 1
        self.loaded = False


class FakeSpeaker(Speaker):
    """A real Speaker whose synthesis and playback are recorded instead of played."""

    def __init__(self, models_dir: Path) -> None:
        super().__init__(models_dir, 1200, play=self._play_fake, stop_playback=self._stop_fake)
        self.played: list[tuple[str, str]] = []
        self.stops = 0

    def synthesize(self, sentence: str, language: str) -> tuple[np.ndarray, int]:
        self.played.append((language, sentence))
        return np.zeros(10, np.float32), 16_000

    def _play_fake(self, audio: np.ndarray, rate: int) -> None:
        pass

    def _stop_fake(self) -> None:
        self.stops += 1


def make_service(
    tmp_path: Path,
    *,
    mic: FakeMic | None = None,
    stt: Any = None,
    free_ram_mb: float = 8000,
    **config: Any,
) -> tuple[VoiceService, FakeMic, Any]:
    mic = mic or FakeMic()
    stt = stt or FakeStt()
    cfg = VoiceConfig(models_dir=tmp_path / "models", **config)
    service = VoiceService(
        cfg,
        stt=stt,
        speaker=FakeSpeaker(tmp_path / "models"),
        stream_factory=mic.factory,
        free_ram_mb=lambda: free_ram_mb,
        debug_dir=tmp_path / "voice_debug",
    )
    return service, mic, stt
