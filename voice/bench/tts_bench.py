"""Benchmark local text-to-speech options for English, Tamil and Sinhala.

    python -m voice.bench.tts_bench

Engines:
- sapi:   Windows built-in voices (SAPI 5) through comtypes. No download.
- mms:    Meta MMS-TTS (VITS) ONNX models run by sherpa-onnx. ~114 MB per language,
          stored in workspace/voice_models/mms-tts/. English and Tamil exist; Sinhala does not.
- espeak: espeak-ng bundled in the espeakng-loader wheel. No download. Robotic but covers si.

Each engine runs in its own process for a clean RAM number. Sample wavs for listening are
written to workspace/voice_bench/tts/. Writes docs/voice_benchmarks_tts.md.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np
import psutil

from voice.bench.common import BENCH_DIR, LANGS, MODELS_DIR, REPO

SENTENCES = {
    "en": "I found three files in your downloads folder. Shall I open the newest one?",
    "ta": "உங்கள் டவுன்லோட்ஸ் போல்டரில் மூன்று கோப்புகள் உள்ளன. புதியதைத் திறக்கட்டுமா?",
    "si": "ඔබේ ඩවුන්ලෝඩ්ස් ෆෝල්ඩරයේ ගොනු තුනක් තියෙනවා. අලුත්ම එක විවෘත කරන්නද?",
}
MMS_REPO = "https://huggingface.co/willwade/mms-tts-multilingual-models-onnx/resolve/main"
MMS_CODES = {"en": "eng", "ta": "tam"}  # no Sinhala MMS-TTS checkpoint exists
OUT_DIR = BENCH_DIR / "tts"


def _save(path: Path, samples: np.ndarray, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(samples, -1, 1) if samples.dtype != np.int16 else samples
    if pcm.dtype != np.int16:
        pcm = (pcm * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


# ---------- engines: each returns {lang: (samples, rate, seconds)} plus voice notes ----------


def run_sapi() -> tuple[dict, list[str]]:
    import comtypes.client

    voice = comtypes.client.CreateObject("SAPI.SpVoice")
    tokens = voice.GetVoices()
    notes, by_lang = [], {}
    lcid_lang = {"409": "en", "809": "en", "449": "ta", "45b": "si"}
    for i in range(tokens.Count):
        tok = tokens.Item(i)
        lang_attr = (tok.GetAttribute("Language") or "").lower()
        lang = next((v for k, v in lcid_lang.items() if k in lang_attr.split(";")), None)
        notes.append(f"{tok.GetDescription()} (LCID {lang_attr}) -> {lang or 'other'}")
        if lang and lang not in by_lang:
            by_lang[lang] = tok
    out = {}
    for lang, tok in by_lang.items():
        stream = comtypes.client.CreateObject("SAPI.SpMemoryStream")
        fmt = comtypes.client.CreateObject("SAPI.SpAudioFormat")
        fmt.Type = 22  # SAFT22kHz16BitMono
        stream.Format = fmt
        voice.Voice = tok
        voice.AudioOutputStream = stream
        t0 = time.perf_counter()
        voice.Speak(SENTENCES[lang], 0)
        secs = time.perf_counter() - t0
        data = bytes(stream.GetData())
        out[lang] = (np.frombuffer(data, dtype=np.int16), 22050, secs)
    return out, notes


def _mms_files(code: str) -> tuple[Path, Path]:
    d = MODELS_DIR / "mms-tts" / code
    d.mkdir(parents=True, exist_ok=True)
    for name in ("model.onnx", "tokens.txt"):
        p = d / name
        if not p.exists():
            urllib.request.urlretrieve(f"{MMS_REPO}/{code}/{name}", p)  # noqa: S310 - fixed https URL
    return d / "model.onnx", d / "tokens.txt"


def run_mms() -> tuple[dict, list[str]]:
    import sherpa_onnx

    out, notes = {}, ["No Sinhala MMS-TTS model exists (facebook/mms-tts-sin is not published)."]
    for lang, code in MMS_CODES.items():
        model, tokens = _mms_files(code)
        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(model=str(model), tokens=str(tokens)),
                num_threads=4,
            )
        )
        tts = sherpa_onnx.OfflineTts(cfg)
        text = SENTENCES[lang].lower() if lang == "en" else SENTENCES[lang]
        t0 = time.perf_counter()
        audio = tts.generate(text, sid=0, speed=1.0)
        secs = time.perf_counter() - t0
        out[lang] = (np.asarray(audio.samples, dtype=np.float32), audio.sample_rate, secs)
    return out, notes


def run_espeak() -> tuple[dict, list[str]]:
    import espeakng_loader

    lib = ctypes.CDLL(str(espeakng_loader.get_library_path()))
    data_parent = str(Path(espeakng_loader.get_data_path()).parent).encode()
    rate = lib.espeak_Initialize(2, 0, data_parent, 0)  # AUDIO_OUTPUT_SYNCHRONOUS
    if rate <= 0:
        raise RuntimeError("espeak_Initialize failed")
    chunks: list[np.ndarray] = []
    cb_type = ctypes.CFUNCTYPE(
        ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_void_p
    )

    def _cb(wav, n, _events):
        if n > 0:
            chunks.append(np.ctypeslib.as_array(wav, shape=(n,)).copy())
        return 0

    cb = cb_type(_cb)
    lib.espeak_SetSynthCallback(cb)
    out = {}
    for lang, text in SENTENCES.items():
        chunks.clear()
        if lib.espeak_SetVoiceByName(lang.encode()) != 0:
            continue
        raw = text.encode("utf-8")
        t0 = time.perf_counter()
        lib.espeak_Synth(raw, len(raw) + 1, 0, 1, 0, 1, None, None)  # espeakCHARS_UTF8
        lib.espeak_Synchronize()
        secs = time.perf_counter() - t0
        out[lang] = (np.concatenate(chunks) if chunks else np.zeros(0, np.int16), rate, secs)
    lib.espeak_Terminate()
    return out, []


ENGINES = {"sapi": run_sapi, "mms": run_mms, "espeak": run_espeak}


def worker(engine: str) -> dict:
    proc = psutil.Process()
    before = proc.memory_info().rss
    res, notes = ENGINES[engine]()
    mi = proc.memory_info()
    langs = {}
    for lang, (samples, rate, secs) in res.items():
        dur = len(samples) / rate if rate else 0
        wav = OUT_DIR / f"{engine}_{lang}.wav"
        _save(wav, samples, rate)
        langs[lang] = {
            "seconds": round(secs, 2),
            "audio_seconds": round(dur, 2),
            "rtf": round(secs / dur, 3) if dur else None,
            "wav": str(wav.relative_to(REPO)),
        }
    return {
        "engine": engine,
        "ram_added_mb": round((mi.rss - before) / 2**20),
        "ram_peak_mb": round(getattr(mi, "peak_wset", mi.rss) / 2**20),
        "langs": langs,
        "notes": notes,
    }


def summarize(results: list[dict]) -> str:
    lines = [
        "# Step 14 — TTS benchmark",
        "",
        "Same sentence per language (about 5 s of speech). RTF = synthesis time / audio length "
        "(below 1 = faster than real time). Listen to the wav files to judge quality.",
        "",
        "| Engine | English | Tamil | Sinhala | RAM added MB | Download |",
        "|---|---|---|---|---|---|",
    ]
    dl = {
        "sapi": "none (Windows)",
        "mms": "~114 MB per language",
        "espeak": "none (in the pip wheel, ~10 MB)",
    }
    for r in results:
        cells = []
        for lang in LANGS:
            x = r["langs"].get(lang)
            cells.append(f"yes, {x['seconds']} s (RTF {x['rtf']})" if x else "no")
        lines.append(
            f"| {r['engine']} | "
            + " | ".join(cells)
            + f" | {r['ram_added_mb']} | {dl[r['engine']]} |"
        )
    lines += [
        "| piper | yes (many voices) | no | no | — | ~60 MB per voice |",
        "",
        "Piper has no Tamil or Sinhala voice (checked rhasspy/piper-voices). Windows can add a "
        "Tamil voice (Microsoft Valluvar) through Settings > Language, which is a system change; "
        "Windows has no Sinhala voice.",
        "",
        "## Notes",
        "",
    ]
    for r in results:
        for n in r["notes"]:
            lines.append(f"- {r['engine']}: {n}")
        for lang, x in r["langs"].items():
            lines.append(
                f"- {r['engine']} {LANGS[lang]}: `{x['wav']}` ({x['audio_seconds']} s audio)"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--engines", nargs="+", default=list(ENGINES), choices=list(ENGINES))
    ap.add_argument("--worker", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: S110 - console keeps its encoding
        pass
    if args.worker:
        print(json.dumps(worker(args.worker), ensure_ascii=False))
        return 0
    results = []
    for e in args.engines:
        print(f"--- {e} ...", flush=True)
        out = subprocess.run(  # noqa: S603 - fixed argv, this module
            [sys.executable, "-m", "voice.bench.tts_bench", "--worker", e],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if out.returncode != 0:
            print(out.stderr[-2000:])
            continue
        results.append(json.loads(out.stdout.strip().splitlines()[-1]))
    report = summarize(results)
    (REPO / "docs" / "voice_benchmarks_tts.md").write_text(report + "\n", "utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
