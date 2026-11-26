"""Cloud model from the command line (owner only).

    python -m model.serving.cloud status
    python -m model.serving.cloud set-key <profile>         # asks for the key, hidden input
    python -m model.serving.cloud delete-key <profile>
    python -m model.serving.cloud configure --preset P --model M [--base-url URL]
            [--budget USD] [--price-in USD] [--price-out USD]   # saves and uses that profile
    python -m model.serving.cloud mode local|hybrid|cloud   # hybrid/cloud show the warning, ask y
    python -m model.serving.cloud test                      # one tiny request

Presets (= profile ids): nvidia, groq, openai, openrouter, deepseek, anthropic, gemini, custom.
The cloud page (/cloud) does all of this without a terminal. Keys go only to the OS
credential store and are read back before "saved" is printed.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import datetime, timezone

from model.serving.cloud.dispatcher import ModelDispatcher, health_value
from model.serving.cloud.keys import KeyStoreError, key_hint
from model.serving.cloud.settings import DATA_LEAVING, PRESETS, Profile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m model.serving.cloud")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    for name in ("set-key", "delete-key"):
        sub.add_parser(name).add_argument("profile")
    conf = sub.add_parser("configure")
    conf.add_argument("--preset", choices=sorted(PRESETS))
    conf.add_argument("--model")
    conf.add_argument("--base-url")
    conf.add_argument("--budget", type=float)
    conf.add_argument("--price-in", type=float)
    conf.add_argument("--price-out", type=float)
    mode = sub.add_parser("mode")
    mode.add_argument("mode", choices=["local", "hybrid", "cloud"])
    sub.add_parser("test")
    args = parser.parse_args(argv)

    d = ModelDispatcher(lambda *a, **k: None)  # never calls the local model here
    settings = d.settings.load()

    if args.cmd == "status":
        ok, value = health_value(d)
        print(value)
        print(json.dumps(d.ledger.this_month().model_dump()))
        return 0
    if args.cmd == "set-key":
        if args.profile not in settings.profiles and args.profile not in PRESETS:
            print(f"unknown profile {args.profile!r}; presets: {', '.join(sorted(PRESETS))}")
            return 1
        key = getpass.getpass(f"API key for {args.profile} (hidden): ")
        try:
            stored = d.keys.set(args.profile, key)  # read back inside; raises if not
        except KeyStoreError as exc:
            print(f"not saved: {exc}")
            return 1
        print(f"saved in the OS credential store and read back (ends with {key_hint(stored)})")
        return 0
    if args.cmd == "delete-key":
        try:
            d.keys.delete(args.profile)
        except KeyStoreError as exc:
            print(f"not removed: {exc}")
            return 1
        print("removed")
        return 0
    if args.cmd == "configure":
        if args.preset:
            preset = PRESETS[args.preset]
            current = settings.profiles.get(args.preset)
            base = current.model_dump() if current else {}
            profile = Profile.model_validate(
                {
                    **base,
                    "label": preset.label,
                    "preset": args.preset,
                    "provider": preset.provider,
                    "base_url": args.base_url or (current.base_url if current else preset.base_url),
                    **({"model": args.model} if args.model is not None else {}),
                    **(
                        {"price_input_per_mtok": args.price_in} if args.price_in is not None else {}
                    ),
                    **(
                        {"price_output_per_mtok": args.price_out}
                        if args.price_out is not None
                        else {}
                    ),
                }
            )
            settings = settings.with_profile(args.preset, profile, activate=True)
        if args.budget is not None:
            settings = settings.model_copy(update={"monthly_budget_usd": args.budget})
        d.settings.save(settings)
        print(
            health_value(d)[1]
            if settings.mode != "local"
            else f"profile: {settings.key_id}, model: {settings.model or '(none)'}, mode: local"
        )
        return 0
    if args.cmd == "mode":
        accepted = settings.warning_accepted_at
        if args.mode == "local":
            accepted = None
        elif settings.mode == "local" or accepted is None:
            if not settings.model:
                print("set a model first: configure --provider P --model M")
                return 1
            print("What leaves this PC when the cloud model is on:")
            for line in DATA_LEAVING:
                print(f"  - {line}")
            if input("Turn the cloud model on? [y/N] ").strip().lower() != "y":
                print("Nothing was changed.")
                return 1
            accepted = datetime.now(timezone.utc).isoformat(timespec="seconds")
        d.settings.save(
            settings.model_copy(update={"mode": args.mode, "warning_accepted_at": accepted})
        )
        from security.audit.audit_logger import get_audit_logger

        get_audit_logger().cloud_call(
            task_id="-",
            agent="owner",
            provider=settings.key_id,
            model=settings.model,
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=0.0,
            outcome=f"mode:{args.mode}",
            duration_ms=0.0,
        )
        print(health_value(d)[1])
        return 0
    if args.cmd == "test":
        print(json.dumps(d.test_connection()))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
