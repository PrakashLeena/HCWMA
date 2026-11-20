"""The voice service (build step 14): push-to-talk -> STT, and replies -> TTS.

- Push-to-talk only. The microphone opens on `start()` and closes on `stop()`, `cancel()`
  or after `max_record_seconds`, whichever comes first. There is no wake word.
- Audio stays in memory and is dropped after transcription. Only when the owner turns on
  `debug_save_audio` in configs/voice.yaml is a copy written to workspace/voice_debug/.
- Whisper is loaded while the owner talks and unloaded after the transcription.
- The transcript goes to the Router as an ordinary chat message (the page sends it), so a
  voice request meets exactly the same tool allowlists and approval prompts. This service has
  no way to answer an approval: approvals are clicks on the approval card only.
"""

from __future__ import annotations

import threading
import time
import wave
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import numpy as np
from pydantic import BaseModel

from voice.activation.push_to_talk import PushToTalkRecorder, StreamFactory, sounddevice_stream
from voice.config import DEBUG_AUDIO_DIR, LANGUAGE_NAMES, VoiceConfig
from voice.memory_budget import (
    QwenRelease,
    close_apps_message,
    qwen_loaded_mb,
    top_apps_by_ram,
)
from voice.memory_budget import (
    free_ram_mb as free_ram_mb_default,
)
from voice.stt.whisper_stt import SAMPLE_RATE, SpeechToText
from voice.tts.speech import Speaker


class VoiceRefused(Exception):
    """Shown to the owner as is."""


class Transcript(BaseModel):
    text: str
    language: str  # the language picked in the app, passed to Whisper (no auto-detect)
    audio_seconds: float
    transcribe_seconds: float
    peak: float  # loudest sample, 0..1: below about 0.03 the microphone barely heard you
    debug_file: str = ""  # only when debug_save_audio is on
    qwen_unloaded: bool = False  # Qwen was unloaded to make room for Whisper, then reloaded
    qwen_unload_s: float = 0.0
    qwen_reload_s: float = 0.0


class VoiceStatus(BaseModel):
    available: bool
    reason: str = ""
    listening: bool
    speaking: bool
    stt_loaded: bool
    stt_model: str
    languages: dict[str, str]
    tts_engines: dict[str, str]
    max_record_seconds: float
    debug_save_audio: bool


class VoiceService:
    def __init__(
        self,
        config: VoiceConfig,
        stt: SpeechToText | None = None,
        speaker: Speaker | None = None,
        stream_factory: StreamFactory = sounddevice_stream,
        free_ram_mb: Callable[[], float] | None = None,
        qwen_mb: Callable[[], float] = qwen_loaded_mb,
        release_factory: Callable[[], QwenRelease] = QwenRelease,
        top_apps: Callable[[], list[tuple[str, int]]] = top_apps_by_ram,
        debug_dir: Path = DEBUG_AUDIO_DIR,
    ) -> None:
        self.config = config
        models = config.models_path()
        self.stt = stt or SpeechToText(config.stt_model, models, config.max_transcribe_seconds)
        self.speaker = speaker or Speaker(models, config.speak_max_chars)
        self.recorder = PushToTalkRecorder(config.max_record_seconds, stream_factory)
        self._free_ram_mb = free_ram_mb or free_ram_mb_default
        self._qwen_mb = qwen_mb
        self._release_factory = release_factory
        self._top_apps = top_apps
        self._release: QwenRelease | None = None
        self.debug_dir = debug_dir
        self.language = "en"
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    # ---- status ----

    def status(self) -> VoiceStatus:
        ok = self.stt.available()
        return VoiceStatus(
            available=ok,
            reason=""
            if ok
            else "the speech model is not downloaded: run  python -m voice download",
            listening=self.recorder.recording,
            speaking=self.speaker.speaking,
            stt_loaded=self.stt.loaded,
            stt_model=self.config.stt_model,
            languages=LANGUAGE_NAMES,
            tts_engines={lang: self.speaker.engine_for(lang) for lang in LANGUAGE_NAMES},
            max_record_seconds=self.config.max_record_seconds,
            debug_save_audio=self.config.debug_save_audio,
        )

    # ---- push-to-talk ----

    def start(self, language: str) -> None:
        if language not in LANGUAGE_NAMES:
            raise VoiceRefused(f"unknown language {language!r}")
        if not self.stt.available():
            raise VoiceRefused("the speech model is not downloaded: run  python -m voice download")
        with self._lock:
            if self.recorder.recording:
                return
            release = self._plan_memory()
            self.speaker.quiet()
            self.language = language
            self.recorder.start()
            self._release = release
            if release is not None:
                release.start()  # Qwen unloads while the owner talks
            # Whisper loads while the owner talks (after Qwen is out, when it had to go)
            self.stt.load_in_background(before=release.wait_unloaded if release else None)
            self._timer = threading.Timer(self.config.max_record_seconds, self.recorder.close)
            self._timer.daemon = True
            self._timer.start()

    def _plan_memory(self) -> QwenRelease | None:
        """None when Whisper fits; a QwenRelease when unloading Qwen makes it fit; else refuse.
        Never closes, kills or trims any other process."""
        need = self.config.stt_ram_mb
        free = self._free_ram_mb()
        if self.stt.loaded or free >= need:
            return None
        qwen = self._qwen_mb()
        if qwen > 0 and free + qwen >= need:
            return self._release_factory()
        raise VoiceRefused(close_apps_message(need, free, self._top_apps()))

    def _finish_release(self) -> QwenRelease | None:
        release, self._release = self._release, None
        if release is not None:
            release.finish()  # Qwen reloads now, before the transcript goes to the Router
        return release

    def stop(self) -> Transcript:
        """Close the microphone, transcribe in memory, drop the audio and the model."""
        with self._lock:
            self._cancel_timer()
            audio = self.recorder.stop()
        debug_file, release = "", None
        try:
            if self.config.debug_save_audio and audio.size:
                debug_file = self._save_debug(audio).name
            t0 = time.perf_counter()
            text = self.stt.transcribe(audio, self.language) if audio.size else ""
            transcribe_s = time.perf_counter() - t0
            peak = float(np.abs(audio).max()) if audio.size else 0.0
            seconds = audio.size / SAMPLE_RATE
        finally:
            audio.fill(0)  # the recording is not kept anywhere
            self.stt.unload()  # Whisper out before Qwen comes back
            release = self._finish_release()  # Qwen reloads before the Router call
        return Transcript(
            text=text,
            language=self.language,
            audio_seconds=round(seconds, 2),
            transcribe_seconds=round(transcribe_s, 2),
            peak=round(peak, 3),
            debug_file=debug_file,
            qwen_unloaded=release is not None,
            qwen_unload_s=round(release.unload_s, 2) if release else 0.0,
            qwen_reload_s=round(release.reload_s, 2) if release else 0.0,
        )

    def cancel(self) -> None:
        """Close the microphone and throw away what it heard (Stop everything, window closed)."""
        with self._lock:
            self._cancel_timer()
            audio = self.recorder.stop()
        audio.fill(0)
        self.stt.unload()
        self._finish_release()
        self.speaker.quiet()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _save_debug(self, audio: np.ndarray) -> Path:
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        path = self.debug_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{self.language}.wav"
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm.tobytes())
        return path

    # ---- speech output ----

    def speak(self, text: str, language: str | None = None) -> dict[str, str]:
        return self.speaker.speak(text, language)

    def quiet(self) -> None:
        self.speaker.quiet()
