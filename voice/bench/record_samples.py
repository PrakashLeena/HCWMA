"""Record the owner's benchmark clips: 5 seconds each, English, Tamil, Sinhala.

    python -m voice.bench.record_samples              # all languages
    python -m voice.bench.record_samples --lang ta    # one language
    python -m voice.bench.record_samples --list-mics

Clips go to workspace/voice_bench/<lang>/NN.wav (gitignored) with the prompt text in
workspace/voice_bench/references.tsv. If you said something different, edit that file.
"""

from __future__ import annotations

import argparse
import sys
import wave

import numpy as np
import sounddevice as sd

from voice.activation.push_to_talk import CAPTURE
from voice.bench.common import (
    BENCH_DIR,
    CLIP_SECONDS,
    LANGS,
    PROMPTS,
    SAMPLE_RATE,
    read_references,
    write_references,
)


def record_clip(seconds: float = CLIP_SECONDS) -> np.ndarray:
    audio = sd.rec(int(seconds * SAMPLE_RATE), **CAPTURE)  # same settings as the app
    sd.wait()
    return audio[:, 0]


def save_wav(path, audio: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(audio.tobytes())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--lang", choices=sorted(LANGS), action="append")
    ap.add_argument("--list-mics", action="store_true")
    args = ap.parse_args(argv)
    if args.list_mics:
        print(sd.query_devices())
        return 0

    try:  # Tamil/Sinhala in a Windows console
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: S110 - console keeps its encoding
        pass

    rows = {(r["lang"], r["file"]): r for r in read_references()}
    print(f"Microphone: {sd.query_devices(kind='input')['name']}")
    print("For each line: press Enter, then say it. Recording lasts 5 seconds.")
    print("Type s + Enter to skip a line, q + Enter to stop.\n")
    for lang in args.lang or list(LANGS):
        print(f"=== {LANGS[lang]} ===")
        for i, prompt in enumerate(PROMPTS[lang], 1):
            rel = f"{lang}/{i:02d}.wav"
            print(f"\n[{rel}] Say: {prompt}")
            answer = input("Enter = record > ").strip().lower()
            if answer == "q":
                write_references(list(rows.values()))
                return 0
            if answer == "s":
                continue
            print("  ... recording")
            audio = record_clip()
            peak = int(np.abs(audio).max())
            save_wav(BENCH_DIR / rel, audio)
            rows[(lang, rel)] = {"lang": lang, "file": rel, "text": prompt}
            note = "  (very quiet: check the microphone)" if peak < 1000 else ""
            print(f"  saved, peak level {peak}/32767{note}")
    write_references(list(rows.values()))
    print(f"\nDone. References: {BENCH_DIR / 'references.tsv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
