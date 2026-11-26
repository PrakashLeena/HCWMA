"""Cloud model from the command line (owner only).

    python -m model.serving.cloud status
    python -m model.serving.cloud set-key <provider>        # asks for the key, hidden input
    python -m model.serving.cloud delete-key <provider>
    python -m model.serving.cloud configure --provider P --model M [--base-url URL]
            [--budget USD] [--price-in USD] [--price-out USD]
    python -m model.serving.cloud mode local|hybrid|cloud   # hybrid/cloud show the warning, ask y
    python -m model.serving.cloud test                      # one tiny request

Providers: openai_compat, anthropic, gemini. The key goes only to the OS credential store.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import datetime, timezone

from model.serving.cloud.dispatcher import ModelDispatcher, health_value
from model.serving.cloud.keys import KeyStoreError
from model.serving.cloud.settings import DATA_LEAVING, DEFAULT_BASE_URLS

PROVIDERS = ["openai_compat", "anthropic", "gemini"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m model.serving.cloud")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    for name in ("set-key", "delete-key"):
        sub.add_parser(name).add_argument("provider", choices=PROVIDERS)
    conf = sub.add_parser("configure")
    conf.add_argument("--provider", choices=PROVIDERS)
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
        key = getpass.getpass(f"API key for {args.provider} (hidden): ")
        try:
            d.keys.set(args.provider, key)
        except KeyStoreError as exc:
            print(f"not saved: {exc}")
            return 1
        print("saved in the OS credential store")
        return 0
    if args.cmd == "delete-key":
        d.keys.delete(args.provider)
        print("removed")
        return 0
    if args.cmd == "configure":
        changes: dict[str, object] = {}
        if args.provider:
            changes["provider"] = args.provider
            changes["base_url"] = DEFAULT_BASE_URLS[args.provider]  # type: ignore[index]
        if args.base_url:
            changes["base_url"] = args.base_url
        if args.model is not None:
            changes["model"] = args.model
        if args.budget is not None:
            changes["monthly_budget_usd"] = args.budget
        if args.price_in is not None:
            changes["price_input_per_mtok"] = args.price_in
        if args.price_out is not None:
            changes["price_output_per_mtok"] = args.price_out
        new = settings.model_validate({**settings.model_dump(), **changes})
        d.settings.save(new)
        print(health_value(d)[1])
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
            provider=settings.provider,
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
