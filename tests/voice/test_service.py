"""Step 14: push-to-talk keeps audio in memory, loads Whisper only while needed."""

from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

from tests.voice.fakes import FakeMic, FakeRelease, FakeStt, make_service
from voice.config import REPO_ROOT, load_voice_config
from voice.service import VoiceRefused


def test_shipped_config_is_the_owners_choice_and_debug_is_off() -> None:
    cfg = load_voice_config()
    assert cfg.stt_model == "small"
    assert cfg.debug_save_audio is False
    assert cfg.models_path().is_relative_to(REPO_ROOT / "workspace")


def test_microphone_is_open_only_between_start_and_stop(tmp_path: Path) -> None:
    service, mic, stt = make_service(tmp_path)
    assert not mic.open_now and mic.opened == 0  # nothing listens until the owner asks
    service.start("ta")
    assert mic.open_now and service.status().listening
    result = service.stop()
    assert not mic.open_now and mic.closed == 1 and not service.status().listening
    assert result.text == "show my downloads" and result.language == "ta"
    assert result.audio_seconds == pytest.approx(2.0)
    assert stt.heard == [(32_000, "ta")]


def test_audio_is_never_written_to_disk_unless_debug_is_on(tmp_path: Path) -> None:
    before = {p for p in tmp_path.rglob("*")}
    service, _, _ = make_service(tmp_path)
    service.start("en")
    service.stop()
    after = {p for p in tmp_path.rglob("*")}
    assert after == before
    assert not (tmp_path / "voice_debug").exists()


def test_debug_option_saves_a_copy(tmp_path: Path) -> None:
    service, _, _ = make_service(tmp_path, debug_save_audio=True)
    service.start("en")
    service.stop()
    saved = list((tmp_path / "voice_debug").glob("*-en.wav"))
    assert len(saved) == 1 and saved[0].stat().st_size > 60_000


def test_whisper_is_loaded_while_listening_and_dropped_after(tmp_path: Path) -> None:
    service, _, stt = make_service(tmp_path)
    assert not stt.loaded
    service.start("en")
    assert stt.loaded and stt.loads == 1
    service.stop()
    assert not stt.loaded and stt.unloads == 1


def test_cancel_discards_what_was_heard(tmp_path: Path) -> None:
    service, mic, stt = make_service(tmp_path)
    service.start("en")
    service.cancel()
    assert not mic.open_now and not stt.loaded and stt.heard == []
    assert service.stop().text == ""  # nothing left to transcribe
    assert stt.heard == []


def test_ram_threshold_is_the_measured_whisper_peak_plus_margin() -> None:
    from voice.memory_budget import SAFETY_MARGIN_MB, WHISPER_SMALL_PEAK_MB

    assert (WHISPER_SMALL_PEAK_MB, SAFETY_MARGIN_MB) == (1038, 300)
    assert load_voice_config().stt_ram_mb == 1340  # 1038 + 300, rounded; was 1024 + 1100 = 2124


def test_enough_free_ram_loads_whisper_without_touching_qwen(tmp_path: Path) -> None:
    FakeRelease.log.clear()
    service, _, stt = make_service(tmp_path, free_ram_mb=1400, qwen_mb=3500)
    service.start("en")
    result = service.stop()
    assert FakeRelease.log == [] and not result.qwen_unloaded and stt.heard


def test_low_ram_unloads_qwen_first_and_reloads_it_before_the_router(tmp_path: Path) -> None:
    FakeRelease.log.clear()

    class OrderedStt(FakeStt):
        def load_in_background(self, before=None) -> None:  # type: ignore[no-untyped-def]
            super().load_in_background(before)
            FakeRelease.log.append("whisper load")

        def unload(self) -> None:
            FakeRelease.log.append("whisper unload")
            super().unload()

    service, mic, _ = make_service(tmp_path, stt=OrderedStt(), free_ram_mb=900, qwen_mb=3500)
    service.start("en")
    assert mic.open_now
    result = service.stop()
    assert FakeRelease.log[:5] == [
        "qwen unload",
        "qwen out",
        "whisper load",
        "whisper unload",
        "qwen reload",
    ]
    assert result.qwen_unloaded
    assert (result.qwen_unload_s, result.qwen_reload_s) == (1.5, 4.0)  # shown in the chat


def test_still_too_low_names_the_top_apps_and_closes_nothing(tmp_path: Path) -> None:
    apps = [("chrome", 2100), ("Code", 900), ("Teams", 700), ("ollama", 400), ("explorer", 150)]
    service, mic, stt = make_service(tmp_path, free_ram_mb=600, qwen_mb=300, top_apps=lambda: apps)
    with pytest.raises(VoiceRefused) as exc:
        service.start("en")
    msg = str(exc.value)
    assert "1340 MB" in msg and "600 MB is free" in msg and "Close some of these" in msg
    for name, mb in apps:
        assert f"{name} {mb} MB" in msg
    assert "does not close apps" in msg
    assert mic.opened == 0 and not stt.loaded


def test_listing_apps_by_ram_never_ends_a_process(monkeypatch: pytest.MonkeyPatch) -> None:
    import psutil

    from voice.memory_budget import top_apps_by_ram

    def forbidden(*_a: object, **_k: object) -> None:
        raise AssertionError("HCWMA must never close another process")

    for name in ("kill", "terminate", "suspend", "send_signal"):
        monkeypatch.setattr(psutil.Process, name, forbidden)
    apps = top_apps_by_ram()
    assert 1 <= len(apps) <= 5 and all(mb >= 0 for _, mb in apps)
    assert [mb for _, mb in apps] == sorted((mb for _, mb in apps), reverse=True)


def test_voice_code_never_kills_or_trims_processes() -> None:
    for path in (REPO_ROOT / "voice").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for call in (
            ".kill(",
            ".terminate(",
            "taskkill",
            "EmptyWorkingSet",
            "SetProcessWorkingSetSize",
        ):
            assert call not in text, f"{path} uses {call}"


def test_qwen_release_holds_the_guard_until_finish() -> None:
    from contextlib import contextmanager

    from voice.memory_budget import QwenRelease

    events: list[str] = []

    @contextmanager
    def guard():  # type: ignore[no-untyped-def]
        events.append("unloaded")
        yield True
        events.append("reloaded")

    release = QwenRelease(guard=guard)
    release.start()
    release.wait_unloaded(timeout=2)
    assert events == ["unloaded"]
    release.finish(timeout=2)
    assert events == ["unloaded", "reloaded"]


def test_qwen_release_reports_a_failed_unload() -> None:
    from contextlib import contextmanager

    from voice.memory_budget import QwenRelease

    @contextmanager
    def guard():  # type: ignore[no-untyped-def]
        raise RuntimeError("did not unload")
        yield

    release = QwenRelease(guard=guard)
    release.start()
    with pytest.raises(RuntimeError, match="could not unload"):
        release.wait_unloaded(timeout=2)


def test_qwen_release_uses_the_sandbox_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    from agents.runtime import model_lock
    from integrations.adapters.pc import windows_sandbox
    from voice.memory_budget import default_guard

    class Backend:
        loaded = True

        def runtime_info(self) -> dict[str, object]:
            return {"loaded": [{"size": 1}] if self.loaded else []}

        def unload(self) -> None:
            self.loaded = False

        def load(self) -> float:
            self.loaded = True
            return 0.0

    backend = Backend()
    real = windows_sandbox.model_released
    monkeypatch.setattr(windows_sandbox, "model_released", lambda **kw: real(backend=backend, **kw))
    with default_guard():
        assert model_lock.is_busy() and not backend.loaded
    assert not model_lock.is_busy() and backend.loaded


def test_missing_model_and_unknown_language_are_refused(tmp_path: Path) -> None:
    service, mic, _ = make_service(tmp_path, stt=FakeStt(available=False))
    assert not service.status().available
    assert "python -m voice download" in service.status().reason
    with pytest.raises(VoiceRefused, match="not downloaded"):
        service.start("en")
    service2, _, _ = make_service(tmp_path)
    with pytest.raises(VoiceRefused, match="unknown language"):
        service2.start("fr")
    assert mic.opened == 0


def test_microphone_closes_itself_after_the_limit(tmp_path: Path) -> None:
    service, mic, _ = make_service(tmp_path, mic=FakeMic(seconds=0.5), max_record_seconds=0.2)
    service.start("en")
    deadline = time.monotonic() + 3
    while mic.open_now and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not mic.open_now  # closed by the timer, not by the owner
    assert service.stop().audio_seconds == pytest.approx(0.2)  # capped at the limit


def test_talking_stops_the_assistant_speaking(tmp_path: Path) -> None:
    service, _, _ = make_service(tmp_path)
    service.speak("First sentence. Second sentence.")
    service.start("en")
    service.stop()
    assert service.speaker._generation >= 2  # the reply in progress was cut off


def test_voice_code_has_no_way_to_answer_an_approval() -> None:
    """A spoken "yes" must never approve: no voice module touches the approval service."""
    for path in (REPO_ROOT / "voice").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom | ast.Import):
                names = [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                names += [a.name for a in node.names]
                assert not any("approval" in n for n in names), f"{path} imports {names}"
            if isinstance(node, ast.Attribute):
                assert node.attr != "decide", f"{path} calls .decide"


def test_app_and_benchmark_recorder_capture_the_same_way() -> None:
    import inspect

    from voice.activation.push_to_talk import CAPTURE
    from voice.bench import record_samples

    assert CAPTURE == {"samplerate": 16_000, "channels": 1, "dtype": "int16"}
    assert "**CAPTURE" in inspect.getsource(record_samples.record_clip)


def test_int16_capture_is_scaled_like_the_benchmark() -> None:
    import numpy as np

    from voice.activation.push_to_talk import to_float

    out = to_float(np.array([[16384], [-32768], [0]], dtype=np.int16))
    assert out.dtype == np.float32 and out.tolist() == [0.5, -1.0, 0.0]


def test_transcript_reports_level_and_debug_file(tmp_path: Path) -> None:
    service, _, _ = make_service(tmp_path, debug_save_audio=True)
    service.start("en")
    result = service.stop()
    assert result.peak == pytest.approx(0.1, abs=0.01)
    assert result.debug_file.endswith("-en.wav")


def test_whisper_gets_the_chosen_language_and_vad(tmp_path: Path) -> None:
    import numpy as np

    from voice.stt.whisper_stt import SpeechToText

    class FakeModel:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] = {}

        def transcribe(self, audio: object, **kwargs: object) -> tuple[list[object], None]:
            self.kwargs = kwargs
            return [], None

    stt = SpeechToText("small", tmp_path, max_seconds=5)
    stt._model = FakeModel()
    stt.transcribe(np.zeros(16_000, np.float32), "ta")
    assert stt._model.kwargs["language"] == "ta"  # never auto-detect
    assert stt._model.kwargs["vad_filter"] is True  # silence is not transcribed
