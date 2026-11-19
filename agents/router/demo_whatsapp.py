"""Step 15 (WhatsApp) "Done when" check on the OWNER'S REAL WhatsApp Desktop, run by hand in a
terminal (needs Ollama running and WhatsApp Desktop open and linked):

    python -m agents.router.demo_whatsapp --chat "<a chat name exactly as WhatsApp shows it>"

The real stack: real Router and Communication agent on Qwen, the MCP server as a child process,
WhatsApp Desktop through UI Automation. YOU play the owner: the approval prompt is printed here
and waits for your y / n (nothing is approved for you).

1. "List my WhatsApp chats"              -> names + previews (read-only)
2. "Read my WhatsApp chat with <chat>"   -> opens that ONE chat and reads it (marks it read)
3. "Send a WhatsApp to Message yourself ..." -> the prompt shows the chat and the full text; you
   type y to send ONE message to your own "Message yourself" chat (or n to refuse)
4. Check from WhatsApp itself (not from the model): the last message in your own chat.

Only your own chat is ever sent to. Nothing is deleted.
"""

from __future__ import annotations

import argparse
import sys
import time

import anyio

from agents.router.demo_gmail import ask
from agents.router.factory import open_router
from integrations.adapters.desktop.whatsapp_tools import WhatsAppUnavailable
from integrations.adapters.pc.windows_whatsapp import WindowsWhatsApp
from security.audit.audit_logger import get_audit_logger


async def run(chat: str, steps: set[str]) -> bool:
    wa = WindowsWhatsApp()
    try:
        visible = wa.list_chats(30)
    except WhatsAppUnavailable as exc:
        print(f"Cannot start: {exc}")
        return False
    print(f"WhatsApp Desktop found; {len(visible)} chats visible.")
    stamp = time.strftime("%H:%M:%S")
    text = f"HCWMA step 15 WhatsApp test {stamp}"
    audit = get_audit_logger()
    before = len(audit.read_all())
    answers: list[bool] = []

    async with open_router(approval_timeout_s=300) as router:
        if "list" in steps:
            print("\n--- 1. list chats (read-only) ---")
            await ask(router, "List my WhatsApp chats", owner_answers=False)
        if "read" in steps:
            print(f"\n--- 2. read the chat '{chat}' (read-only) ---")
            await ask(
                router,
                f"Read my WhatsApp chat with {chat} and tell me what the last messages say",
                owner_answers=False,
            )
        if "send" in steps:
            print("\n--- 3. send ONE message to your own chat, only if you approve (5 min) ---")
            _, answers = await ask(
                router,
                f"Send a WhatsApp message to Message yourself saying: {text}",
                owner_answers=True,
            )

    records = [r for r in audit.read_all()[before:] if r.event == "tool_call"]
    print("\n--- 4. what the audit log and WhatsApp itself show ---")
    for r in records:
        print(f"  audit: {r.agent} {r.tool} -> {r.outcome} {r.reason[:70]}")
    calls = {(r.tool, r.outcome) for r in records}
    checks: dict[str, bool] = {}
    if "list" in steps:
        checks["chats listed (whatsapp_list_chats ok)"] = ("whatsapp_list_chats", "ok") in calls
    if "read" in steps:
        checks[f"'{chat}' read (whatsapp_read_chat ok)"] = ("whatsapp_read_chat", "ok") in calls
    if "send" in steps:
        sends = [r for r in records if r.tool == "whatsapp_send"]
        approved = bool(answers and answers[0])
        last = ""
        try:
            wa.open_chat("Message yourself", self_chat=True)
            mine = wa.read_open_chat(5)
            last = mine[-1].text if mine else ""
        except Exception as exc:  # noqa: BLE001
            print(f"  could not read your own chat back: {exc}")
        print(f"  last message in your own chat: {last[:80]!r}")
        checks["the send asked first (prompt shown and answered)"] = bool(answers)
        checks["approved -> the message is in your own chat"] = text in last if approved else True
        checks["denied -> nothing sent"] = text not in last if (answers and not approved) else True
        checks["one whatsapp_send call, audited"] = len(sends) == 1
    print()
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    return all(checks.values())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--chat", required=True, help="a chat name exactly as WhatsApp shows it")
    parser.add_argument("steps", nargs="*", help="any of: list read send (default: all)")
    args = parser.parse_args()
    unknown = set(args.steps) - {"list", "read", "send"}
    if unknown:
        parser.error(f"unknown steps: {', '.join(sorted(unknown))}")
    ok = anyio.run(run, args.chat, set(args.steps) or {"list", "read", "send"})
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
