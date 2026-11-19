"""Text-to-speech for English, Tamil and Sinhala, played on the PC's speaker.

Owner's choice (step 14): MMS-TTS (VITS, run by sherpa-onnx) for English and Tamil,
espeak-ng for Sinhala (no Sinhala MMS model exists) and as the fallback when an MMS model
file is missing. The reply's language is taken from its script. Audio is synthesized into
memory and played; no file is written.
"""

from __future__ import annotations

import ctypes
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

MMS_CODES = {"en": "eng", "ta": "tam"}

_TAMIL = re.compile(r"[஀-௿]")
_SINHALA = re.compile(r"[඀-෿]")


def detect_language(text: str) -> str:
    """'ta' or 'si' when that script appears more than Latin letters, else 'en'."""
    ta, si = len(_TAMIL.findall(text)), len(_SINHALA.findall(text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if ta >= max(si, 1) and ta >= latin / 2:
        return "ta"
    if si >= 1 and si >= latin / 2:
        return "si"
    return "en"


_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()  # noqa: E501
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def number_words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    if n < 1000:
        rest = n % 100
        return _ONES[n // 100] + " hundred" + ("" if rest == 0 else " " + number_words(rest))
    if n < 1_000_000:
        rest = n % 1000
        return (
            number_words(n // 1000) + " thousand" + ("" if rest == 0 else " " + number_words(rest))
        )
    return " ".join(_ONES[int(d)] for d in str(n))


def clean_for_speech(text: str, max_chars: int) -> str:
    """Drop code blocks, links and markdown marks; cut to `max_chars` at a sentence end."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"https?://\S+", " a link ", text)
    text = re.sub(r"[`*_#>|\[\]]", " ", text)
    text = " ".join(text.split())
    if len(text) > max_chars:
        cut = text[:max_chars]
        end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "), cut.rfind("। "))
        text = cut[: end + 1] if end > max_chars // 2 else cut
    return text


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?।])\s+", text)
    return [p for p in (s.strip() for s in parts) if p]


def prepare_english(text: str) -> str:
    """MMS English knows lower-case letters only: spell numbers, lower-case the rest."""
    text = re.sub(r"\d+", lambda m: f" {number_words(int(m.group()))} ", text)
    return " ".join(text.lower().split())


class MmsVoice:
    def __init__(self, model_dir: Path) -> None:
        import sherpa_onnx

        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=str(model_dir / "model.onnx"), tokens=str(model_dir / "tokens.txt")
                ),
                num_threads=4,
            )
        )
        self._tts = sherpa_onnx.OfflineTts(cfg)

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        audio = self._tts.generate(text, sid=0, speed=1.0)
        return np.asarray(audio.samples, dtype=np.float32), int(audio.sample_rate)


class EspeakVoice:
    """espeak-ng from the espeakng-loader wheel, called through ctypes (synchronous mode)."""

    _lock = threading.Lock()

    def __init__(self) -> None:
        import espeakng_loader

        self._lib = ctypes.CDLL(str(espeakng_loader.get_library_path()))
        parent = str(Path(espeakng_loader.get_data_path()).parent).encode()
        self.rate = int(self._lib.espeak_Initialize(2, 0, parent, 0))  # AUDIO_OUTPUT_SYNCHRONOUS
        if self.rate <= 0:
            raise RuntimeError("espeak-ng did not start")
        self._chunks: list[np.ndarray] = []
        cb_type = ctypes.CFUNCTYPE(
            ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_void_p
        )
        self._cb = cb_type(self._collect)  # keep a reference: espeak calls it later
        self._lib.espeak_SetSynthCallback(self._cb)

    def _collect(self, wav: Any, n: int, _events: Any) -> int:
        if n > 0:
            self._chunks.append(np.ctypeslib.as_array(wav, shape=(n,)).astype(np.float32) / 32768)
        return 0

    def synthesize(self, text: str, language: str) -> tuple[np.ndarray, int]:
        with self._lock:
            self._chunks = []
            self._lib.espeak_SetVoiceByName(language.encode())
            raw = text.encode("utf-8")
            self._lib.espeak_Synth(raw, len(raw) + 1, 0, 1, 0, 1, None, None)  # UTF-8 text
            self._lib.espeak_Synchronize()
            chunks, self._chunks = self._chunks, []
        audio = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
        return audio, self.rate


def sounddevice_play(audio: np.ndarray, rate: int) -> None:
    import sounddevice as sd

    sd.play(audio, rate)
    sd.wait()


def sounddevice_stop() -> None:
    import sounddevice as sd

    sd.stop()


class Speaker:
    """Speaks one reply at a time in a background thread; `quiet()` stops it at once."""

    def __init__(
        self,
        models_dir: Path,
        max_chars: int,
        play: Callable[[np.ndarray, int], None] = sounddevice_play,
        stop_playback: Callable[[], None] = sounddevice_stop,
    ) -> None:
        self.mms_dir = models_dir / "mms-tts"
        self.max_chars = max_chars
        self._play, self._stop_playback = play, stop_playback
        self._mms: dict[str, MmsVoice] = {}
        self._espeak: EspeakVoice | None = None
        self._generation = 0
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def engine_for(self, language: str) -> str:
        code = MMS_CODES.get(language)
        if code and (self.mms_dir / code / "model.onnx").exists():
            return "mms"
        return "espeak"

    def synthesize(self, sentence: str, language: str) -> tuple[np.ndarray, int]:
        if self.engine_for(language) == "mms":
            voice = self._mms.get(language)
            if voice is None:
                voice = self._mms[language] = MmsVoice(self.mms_dir / MMS_CODES[language])
            text = prepare_english(sentence) if language == "en" else sentence
            return voice.synthesize(re.sub(r"[.!?।]", " ", text).strip())  # not MMS tokens
        if self._espeak is None:
            self._espeak = EspeakVoice()
        return self._espeak.synthesize(sentence, language)

    def speak(self, text: str, language: str | None = None) -> dict[str, str]:
        """Start speaking `text` (stops whatever was being said). Returns at once."""
        clean = clean_for_speech(text, self.max_chars)
        lang = language or detect_language(clean)
        self.quiet()
        with self._lock:
            self._generation += 1
            gen = self._generation
        if clean:
            self._thread = threading.Thread(
                target=self._run, args=(clean, lang, gen), name="tts", daemon=True
            )
            self._thread.start()
        return {"language": lang, "engine": self.engine_for(lang)}

    def _run(self, text: str, language: str, gen: int) -> None:
        for sentence in split_sentences(text):
            if gen != self._generation:
                return
            try:
                audio, rate = self.synthesize(sentence, language)
            except Exception:  # a voice that fails is skipped, the app keeps running
                return
            if gen != self._generation or audio.size == 0:
                return
            self._play(audio, rate)

    @property
    def speaking(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def quiet(self) -> None:
        with self._lock:
            self._generation += 1
        if self.speaking:
            self._stop_playback()
