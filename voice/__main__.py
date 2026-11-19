"""Voice set-up and checks (build step 14).

    python -m voice download        # Whisper small (~484 MB) + MMS English and Tamil (~114 MB each)
    python -m voice status          # what is downloaded, which engine speaks each language
    python -m voice say "text"      # speak a line on this PC's speaker (language from the script)

Models go to `models_dir` in configs/voice.yaml (default workspace/voice_models/, gitignored).
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.request

from voice.config import LANGUAGE_NAMES, load_voice_config
from voice.service import VoiceService
from voice.tts.speech import MMS_CODES

MMS_URL = "https://huggingface.co/willwade/mms-tts-multilingual-models-onnx/resolve/main"


def download() -> int:
    from faster_whisper import download_model

    cfg = load_voice_config()
    models = cfg.models_path()
    print(f"Whisper {cfg.stt_model} -> {models / cfg.stt_model}")
    download_model(cfg.stt_model, output_dir=str(models / cfg.stt_model))
    for lang, code in MMS_CODES.items():
        target = models / "mms-tts" / code
        target.mkdir(parents=True, exist_ok=True)
        for name in ("tokens.txt", "model.onnx"):
            path = target / name
            if not path.exists():
                print(f"MMS {LANGUAGE_NAMES[lang]}: {name}")
                tmp = path.with_suffix(path.suffix + ".part")
                urllib.request.urlretrieve(f"{MMS_URL}/{code}/{name}", tmp)  # noqa: S310 - fixed https URL
                tmp.replace(path)
    print("Done. Sinhala speech uses espeak-ng, which comes with the espeakng-loader package.")
    return 0


def status() -> int:
    info = VoiceService(load_voice_config()).status()
    print(f"Speech-to-text: Whisper {info.stt_model}, {'ready' if info.available else info.reason}")
    for lang, engine in info.tts_engines.items():
        print(f"Speech ({LANGUAGE_NAMES[lang]}): {engine}")
    print(f"Push-to-talk limit: {info.max_record_seconds:g} s")
    print(f"Debug copies of recordings: {'ON' if info.debug_save_audio else 'off'}")
    return 0


def say(text: str) -> int:
    service = VoiceService(load_voice_config())
    started = service.speak(text)
    print(f"Speaking ({LANGUAGE_NAMES[started['language']]}, {started['engine']}) ...")
    while service.speaker.speaking:
        time.sleep(0.1)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: S110 - a console that cannot switch keeps its encoding
        pass
    ap = argparse.ArgumentParser(
        prog="python -m voice", description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("download")
    sub.add_parser("status")
    say_p = sub.add_parser("say")
    say_p.add_argument("text")
    args = ap.parse_args(argv)
    if args.command == "download":
        return download()
    if args.command == "status":
        return status()
    return say(args.text)


if __name__ == "__main__":
    raise SystemExit(main())
