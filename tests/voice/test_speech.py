"""Step 14: which voice speaks a reply, and how the text is prepared."""

from __future__ import annotations

import time
from pathlib import Path

from tests.voice.fakes import FakeSpeaker
from voice.tts.speech import (
    clean_for_speech,
    detect_language,
    number_words,
    prepare_english,
    split_sentences,
)


def test_language_comes_from_the_script() -> None:
    assert detect_language("I found three files.") == "en"
    assert detect_language("மூன்று கோப்புகள் உள்ளன") == "ta"
    assert detect_language("ගොනු තුනක් තියෙනවා") == "si"
    assert detect_language("உங்கள் invoice file கிடைத்தது") == "ta"
    assert detect_language("") == "en"


def test_english_numbers_are_spelled_for_mms() -> None:
    assert number_words(9) == "nine"
    assert number_words(42) == "forty two"
    assert number_words(2026) == "two thousand twenty six"
    assert prepare_english("Remind me at 9 AM.") == "remind me at nine am."


def test_code_links_and_markdown_are_not_read_aloud() -> None:
    text = "**Done.** See https://example.com/x\n```python\nprint(1)\n```\nAll good."
    spoken = clean_for_speech(text, 1200)
    assert "print" not in spoken and "https" not in spoken and "*" not in spoken
    assert "Done." in spoken and "a link" in spoken and "All good." in spoken


def test_long_replies_are_cut_at_a_sentence_end() -> None:
    text = "This is one sentence. " * 100
    spoken = clean_for_speech(text, 200)
    assert len(spoken) <= 200 and spoken.endswith(".")


def test_sentences_are_split_so_stop_is_quick() -> None:
    assert split_sentences("One. Two? Three!") == ["One.", "Two?", "Three!"]


def test_engine_choice_mms_for_en_ta_espeak_for_si(tmp_path: Path) -> None:
    models = tmp_path / "models"
    speaker = FakeSpeaker(models)
    assert {lang: speaker.engine_for(lang) for lang in ("en", "ta", "si")} == {
        "en": "espeak",  # MMS not downloaded: espeak-ng is the fallback
        "ta": "espeak",
        "si": "espeak",
    }
    for code in ("eng", "tam"):
        (models / "mms-tts" / code).mkdir(parents=True)
        (models / "mms-tts" / code / "model.onnx").write_bytes(b"x")
    assert speaker.engine_for("en") == "mms" and speaker.engine_for("ta") == "mms"
    assert speaker.engine_for("si") == "espeak"  # no Sinhala MMS model exists


def test_speaker_speaks_each_sentence_and_quiet_stops(tmp_path: Path) -> None:
    speaker = FakeSpeaker(tmp_path)
    started = speaker.speak("மூன்று கோப்புகள். புதியதைத் திறக்கட்டுமா?")
    assert started["language"] == "ta"
    deadline = time.monotonic() + 3
    while speaker.speaking and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [s for _, s in speaker.played] == ["மூன்று கோப்புகள்.", "புதியதைத் திறக்கட்டுமா?"]
    speaker.quiet()
    assert not speaker.speaking
