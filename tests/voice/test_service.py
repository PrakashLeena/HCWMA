"""Step 14: push-to-talk keeps audio in memory, loads Whisper only while needed."""

from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

from tests.voice.fakes import FakeMic, FakeStt, make_service
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


def test_low_ram_refuses_to_load_whisper(tmp_path: Path) -> None:
    service, mic, _ = make_service(tmp_path, free_ram_mb=1500, stt_ram_mb=1100)
    with pytest.raises(VoiceRefused, match="not enough free RAM"):
        service.start("en", min_free_ram_mb=1024)
    assert mic.opened == 0


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
