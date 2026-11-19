"""Shared paths, prompts and scoring for the voice benchmarks."""

from __future__ import annotations

import csv
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BENCH_DIR = REPO / "workspace" / "voice_bench"
MODELS_DIR = REPO / "workspace" / "voice_models"
REFERENCES = BENCH_DIR / "references.tsv"
SAMPLE_RATE = 16_000
CLIP_SECONDS = 5.0

LANGS = {"en": "English", "ta": "Tamil", "si": "Sinhala"}

# Default prompts. The owner may say them differently: fix the text in references.tsv.
PROMPTS: dict[str, list[str]] = {
    "en": [
        "Show me the files in my downloads folder",
        "Remind me tomorrow at nine in the morning",
        "Read my latest email",
        "Make this photo smaller",
        "Where is my invoice file",
    ],
    "ta": [
        "என் டவுன்லோட்ஸ் போல்டரில் உள்ள கோப்புகளைக் காட்டு",
        "நாளை காலை ஒன்பது மணிக்கு எனக்கு நினைவூட்டு",
        "என் சமீபத்திய மின்னஞ்சலைப் படி",
        "இந்த புகைப்படத்தின் அளவைக் குறை",
        "என் invoice file எங்கே இருக்கு",
    ],
    "si": [
        "මගේ ඩවුන්ලෝඩ්ස් ෆෝල්ඩරයේ ඇති ගොනු පෙන්වන්න",
        "හෙට උදේ නවයට මට මතක් කරන්න",
        "මගේ අලුත්ම ඊමේල් එක කියවන්න",
        "මේ ඡායාරූපයේ ප්‍රමාණය අඩු කරන්න",
        "මගේ invoice file එක කොහෙද",
    ],
}


def normalize(text: str) -> str:
    """Lower-case, drop punctuation, collapse spaces. Keeps combining marks and ZWJ."""
    kept = []
    for ch in text.lower():
        cat = unicodedata.category(ch)
        if cat.startswith("P") or cat.startswith("S"):
            kept.append(" ")
        else:
            kept.append(ch)
    return " ".join("".join(kept).split())


def _edit_distance(a: list[str], b: list[str]) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def wer(ref: str, hyp: str) -> float:
    r, h = normalize(ref).split(), normalize(hyp).split()
    return _edit_distance(r, h) / max(len(r), 1)


def cer(ref: str, hyp: str) -> float:
    r = list(normalize(ref).replace(" ", ""))
    h = list(normalize(hyp).replace(" ", ""))
    return _edit_distance(r, h) / max(len(r), 1)


def read_references() -> list[dict[str, str]]:
    """Rows: lang, file, text. Only rows whose wav exists."""
    if not REFERENCES.exists():
        return []
    with REFERENCES.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    return [r for r in rows if (BENCH_DIR / r["file"]).exists()]


def write_references(rows: list[dict[str, str]]) -> None:
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    with REFERENCES.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["lang", "file", "text"], delimiter="\t")
        w.writeheader()
        w.writerows(rows)
