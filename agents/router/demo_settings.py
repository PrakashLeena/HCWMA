"""System Settings live check (owner decision 2026-11-19), through the RUNNING desktop app:

    python -m app.desktop              (first, in another terminal; new code)
    python -m agents.router.demo_settings   (~3-5 min on CPU, needs Ollama)

Every request goes to the desktop app's own backend on 127.0.0.1 with its per-install token,
so the real Router, agent, tool server, audit log and approval window are used. The two
Bluetooth changes stop at the approval prompt IN THE DESKTOP APP WINDOW: click Approve there.
Nothing here approves anything by itself.

1. "What are my current settings?"   -> status, worded by code (no approval)
2. "Turn Bluetooth <off|on>"         -> approval prompt in the app -> Bluetooth really switched
3. "Turn Bluetooth <on|off>"         -> approval prompt again -> back to how it started
4. "Turn on Do Not Disturb"          -> an honest "cannot" (no tool exists), nothing changed
   (--skip-refusal skips this one model call)

The order of 2 and 3 depends on Bluetooth's state at the start, so the PC ends as it began.
"""

from __future__ import annotations

import argparse
import sys
import time

from agents.router.demo_planner_docs import App
from integrations.adapters.pc.windows_settings import WindowsSettings


def _bluetooth(windows: WindowsSettings) -> bool | None:
    return windows.status().bluetooth


def run(skip_refusal: bool) -> bool:
    app = App()
    try:
        app.call("GET", "/approvals", timeout=5)
    except (OSError, ValueError):
        print(f"The desktop app is not running on {app.base}. Start: python -m app.desktop")
        return False

    windows = WindowsSettings()
    start = _bluetooth(windows)
    if start is None:
        print("This PC has no Bluetooth radio Windows can switch; nothing to demo.")
        return False
    checks: list[tuple[str, bool]] = []

    print("\n--- 1. status (read only, worded by code) ---")
    r1 = app.chat("What are my current settings?")
    checks.append(("1 routed to system_settings", "system_settings" in r1["agents"]))
    checks.append(("1 status shown", "Bluetooth:" in r1["reply"] and "Volume:" in r1["reply"]))

    first, second = ("off", "on") if start else ("on", "off")
    for n, state in ((2, first), (3, second)):
        print(f"\n--- {n}. Bluetooth {state}: APPROVE the prompt in the HCWMA window ---")
        reply = app.chat(f"Turn Bluetooth {state}")
        time.sleep(1)
        now = _bluetooth(windows)
        print(f"  Windows reports Bluetooth: {'on' if now else 'off'}")
        checks.append((f"{n} routed to system_settings", "system_settings" in reply["agents"]))
        checks.append((f"{n} Bluetooth is really {state}", now is (state == "on")))

    if not skip_refusal:
        print("\n--- 4. Do Not Disturb: no tool exists, an honest answer ---")
        before = windows.status()
        r4 = app.chat("Turn on Do Not Disturb")
        checks.append(("4 nothing changed", windows.status() == before))
        checks.append(("4 no tool call", r4["tool_calls"] == 0))

    print("\n=== results ===")
    for label, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    print(f"Bluetooth started {'on' if start else 'off'} and is now "
          f"{'on' if _bluetooth(windows) else 'off'}.")  # fmt: skip
    return all(ok for _, ok in checks)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--skip-refusal", action="store_true")
    args = parser.parse_args()
    return 0 if run(args.skip_refusal) else 1


if __name__ == "__main__":
    sys.exit(main())
