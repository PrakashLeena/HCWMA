"""Benchmark local speech-to-text (faster-whisper, CPU, int8) on the owner's clips.

    python -m voice.bench.stt_bench                       # tiny, base, small
    python -m voice.bench.stt_bench --models tiny base
    python -m voice.bench.stt_bench --models medium --allow-large   # 1.5 GB download

Each model runs in its own process so its RAM is measured on its own.
Models are stored in workspace/voice_models/ (gitignored).
Writes workspace/voice_bench/stt_results.json and docs/voice_benchmarks_stt.md.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np
import psutil

from voice.bench.common import BENCH_DIR, LANGS, MODELS_DIR, REPO, cer, read_references, wer

# Approximate download sizes (MB) of Systran/faster-whisper-* checkpoints.
MODEL_MB = {"tiny": 75, "base": 145, "small": 484, "medium": 1530, "large-v3-turbo": 1620}
LARGE_LIMIT_MB = 500


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def worker(model_name: str) -> dict:
    """Runs in a child process: load one model, transcribe every clip, report RAM/time."""
    from faster_whisper import WhisperModel, download_model

    proc = psutil.Process()
    rss_before = proc.memory_info().rss
    path = download_model(model_name, output_dir=str(MODELS_DIR / model_name))
    t0 = time.perf_counter()
    model = WhisperModel(path, device="cpu", compute_type="int8")
    load_s = time.perf_counter() - t0
    rss_loaded = proc.memory_info().rss

    clips = []
    for row in read_references():
        audio = load_wav(BENCH_DIR / row["file"])
        t0 = time.perf_counter()
        segs, _ = model.transcribe(
            audio,
            language=row["lang"],
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        text = " ".join(s.text.strip() for s in segs)
        secs = time.perf_counter() - t0
        detected, prob, _ = model.detect_language(audio)
        clips.append(
            {
                "lang": row["lang"],
                "file": row["file"],
                "ref": row["text"],
                "hyp": text,
                "seconds": round(secs, 2),
                "audio_seconds": round(len(audio) / 16000, 2),
                "wer": round(wer(row["text"], text), 3),
                "cer": round(cer(row["text"], text), 3),
                "detected": detected,
                "detected_prob": round(float(prob), 2),
            }
        )
    mi = proc.memory_info()
    peak = getattr(mi, "peak_wset", mi.rss)
    return {
        "model": model_name,
        "load_seconds": round(load_s, 2),
        "ram_model_mb": round((rss_loaded - rss_before) / 2**20),
        "ram_peak_mb": round(peak / 2**20),
        "clips": clips,
    }


def summarize(results: list[dict]) -> str:
    vm = psutil.virtual_memory()
    lines = [
        "# Step 14 — STT benchmark (owner's own recordings)",
        "",
        f"faster-whisper, CPU, int8, greedy (beam 1), VAD on. {psutil.cpu_count()} logical CPUs, "
        f"{vm.total / 2**30:.1f} GB RAM ({vm.available / 2**30:.1f} GB free when run).",
        "Accuracy: WER = word error rate, CER = character error rate (lower is better; "
        "CER is the fairer score for Tamil and Sinhala). Lang ID = clips where auto-detect "
        "picked the right language.",
        "",
        "| Model | Language | Clips | Mean s / 5 s clip | Max s | WER | CER | Lang ID | Peak RAM MB |",  # noqa: E501
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        for lang, name in LANGS.items():
            cl = [c for c in r["clips"] if c["lang"] == lang]
            if not cl:
                continue
            secs = [c["seconds"] for c in cl]
            lines.append(
                f"| {r['model']} | {name} | {len(cl)} | {statistics.mean(secs):.2f} | {max(secs):.2f} | "  # noqa: E501
                f"{statistics.mean(c['wer'] for c in cl):.0%} | {statistics.mean(c['cer'] for c in cl):.0%} | "  # noqa: E501
                f"{sum(c['detected'] == lang for c in cl)}/{len(cl)} | {r['ram_peak_mb']} |"
            )
    lines += ["", "Model load time and RAM added by the model:", ""]
    for r in results:
        lines.append(f"- {r['model']}: load {r['load_seconds']} s, +{r['ram_model_mb']} MB")
    lines += ["", "## What each model heard", ""]
    for r in results:
        lines.append(f"### {r['model']}")
        lines.append("")
        for c in r["clips"]:
            lines.append(f"- `{c['file']}` CER {c['cer']:.0%}: {c['hyp'] or '(nothing)'}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--models", nargs="+", default=["tiny", "base", "small"])
    ap.add_argument(
        "--allow-large", action="store_true", help=f"allow downloads over {LARGE_LIMIT_MB} MB"
    )
    ap.add_argument("--worker", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: S110 - console keeps its encoding
        pass

    if args.worker:
        print(json.dumps(worker(args.worker), ensure_ascii=False))
        return 0

    if not read_references():
        print("No recordings yet. Run: python -m voice.bench.record_samples")
        return 1
    for m in args.models:
        if MODEL_MB.get(m, 10_000) > LARGE_LIMIT_MB and not args.allow_large:
            print(f"{m} is about {MODEL_MB.get(m, '?')} MB: ask the owner, then add --allow-large.")
            return 2

    results = []
    for m in args.models:
        print(f"--- {m} (download ~{MODEL_MB.get(m, '?')} MB if not cached) ...", flush=True)
        out = subprocess.run(  # noqa: S603 - fixed argv, this module
            [sys.executable, "-m", "voice.bench.stt_bench", "--worker", m],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if out.returncode != 0:
            print(out.stderr[-2000:])
            return 1
        res = json.loads(out.stdout.strip().splitlines()[-1])
        results.append(res)
        print(f"    peak RAM {res['ram_peak_mb']} MB, load {res['load_seconds']} s", flush=True)

    (BENCH_DIR / "stt_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), "utf-8"
    )
    report = summarize(results)
    (REPO / "docs" / "voice_benchmarks_stt.md").write_text(report + "\n", "utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
