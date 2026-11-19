"""Step 15 "Done when" check on the OWNER'S REAL GMAIL, run by hand in a terminal (needs Ollama
running, the Gmail login done with `python -m integrations.adapters.official_api.gmail_login`):

    python -m agents.router.demo_gmail

The real stack: real Router and Communication agent on Qwen, the MCP server as a child process,
the real Gmail API and the Windows credential store. YOU play the owner: the approval prompt is
printed here and waits for your y / n (nothing is approved for you).

1. "Read my latest email"  -> search + read (read-only; nothing changes)
2. "Draft an email to <your own address> ..." -> a draft appears in Gmail, nothing is sent
3. "Send an email to <your own address> ..." -> the prompt shows To, Subject and the full text;
   you type y to send ONE email to yourself (or n to refuse)
4. Check from Gmail itself (not from the model): the draft exists, the sent mail exists (or not)

Only your own address is ever used. Nothing is deleted.
"""

from __future__ import annotations

import sys
import time

import anyio

from agents.router.factory import open_router
from agents.router.router import Router, RouterReply
from integrations.adapters.official_api.gmail_api import GmailApi
from integrations.adapters.official_api.gmail_auth import GmailLoginNeeded
from model.serving import Message
from security.approvals.approval_service import PendingApproval
from security.audit.audit_logger import get_audit_logger

DRAFT_SUBJECT = "HCWMA step 15 draft test"
SEND_SUBJECT = "HCWMA step 15 send test"


def _show(reply: RouterReply) -> None:
    path = " -> ".join(["router", *reply.agents])
    print(f"  [{path} | {reply.action} | {reply.status} | tool calls {reply.tool_calls}]")
    for line in reply.reply.strip().splitlines():
        print(f"  {line}")


async def ask(router: Router, text: str, *, owner_answers: bool) -> tuple[RouterReply, list[bool]]:
    """`owner_answers`: show any approval prompt here and wait for the real owner's y / n."""
    approvals = router.approvals
    if approvals is None:
        raise RuntimeError("this Router was built without the approval service")
    answers: list[bool] = []
    print(f"\n> {text}")

    async def play_owner() -> None:
        while not approvals.pending():
            await anyio.sleep(0.1)
        prompt: PendingApproval = approvals.pending()[0]
        who = f"{prompt.agent} -> {prompt.tool}, tier {prompt.tier.value}"
        print(f"\n  ===== APPROVAL PROMPT ({who}) =====")
        for line in prompt.summary.splitlines():
            print(f"  | {line}")
        print("  =====")
        reply = await anyio.to_thread.run_sync(
            lambda: input("  Send it? Type y to approve, anything else to deny: ")
        )
        approved = reply.strip().lower() in ("y", "yes")
        answers.append(approved)
        approvals.decide(prompt.id, approved)

    start = time.perf_counter()
    async with anyio.create_task_group() as tg:
        if owner_answers:
            tg.start_soon(play_owner)
        reply = await router.handle([Message(role="user", content=text)])
        tg.cancel_scope.cancel()
    print(f"  ({time.perf_counter() - start:.0f} s)")
    _show(reply)
    return reply, answers


async def run(steps: set[str]) -> bool:
    api = GmailApi()
    try:
        me = api.profile_address()
    except GmailLoginNeeded as exc:
        print(f"Cannot start: {exc}.")
        return False
    audit = get_audit_logger()
    before = len(audit.read_all())
    print(f"Gmail account: {me} (the only address this demo uses)")

    read: RouterReply | None = None
    answers: list[bool] = []
    async with open_router(approval_timeout_s=300) as router:
        if "read" in steps:
            print("\n--- 1. search and read the latest email (read-only) ---")
            read, _ = await ask(
                router,
                "Read my latest email and tell me who it is from and the subject",
                owner_answers=False,
            )
        if "draft" in steps:
            print("\n--- 2. draft (nothing is sent) ---")
            await ask(
                router,
                f"Draft an email to {me} with the subject '{DRAFT_SUBJECT}' and the text "
                "'This is a draft made by HCWMA. Nothing was sent.'",
                owner_answers=False,
            )
        if "send" in steps:
            print("\n--- 3. send ONE email to yourself, only if you approve (5 min) ---")
            _, answers = await ask(
                router,
                f"Send an email to {me} with the subject '{SEND_SUBJECT}' and the text "
                "'This is the one test email HCWMA sent after you approved it.'",
                owner_answers=True,
            )

    print("\n--- 4. what Gmail itself shows ---")
    drafts = api.search(f'in:drafts subject:"{DRAFT_SUBJECT}"', 3)
    sent = api.search(f'in:sent subject:"{SEND_SUBJECT}"', 3)
    print(f"  drafts named '{DRAFT_SUBJECT}': {len(drafts)}")
    print(f"  sent mails named '{SEND_SUBJECT}': {len(sent)}")
    new_records = audit.read_all()[before:]
    sends = [r for r in new_records if r.event == "tool_call" and r.tool == "send_message"]
    for r in new_records:
        if r.event == "tool_call":
            print(f"  audit: {r.agent} {r.tool} -> {r.outcome} {r.reason[:60]}")

    approved = bool(answers and answers[0])
    checks: dict[str, bool] = {"a draft exists in Gmail": len(drafts) >= 1}
    if read is not None:
        checks["latest email read through search_messages + read_message"] = (
            read.tool_calls >= 2 and read.status == "done"
        )
    if "send" in steps:
        checks["the send asked first (prompt shown and answered)"] = bool(answers)
        checks["approved -> exactly one email sent"] = len(sent) == 1 if approved else True
        checks["denied -> nothing sent"] = len(sent) == 0 if (answers and not approved) else True
        checks["one send_message call, audited"] = len(sends) == 1
    print()
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    return all(checks.values())


def main() -> int:
    """`python -m agents.router.demo_gmail [read] [draft] [send]` runs only those steps."""
    chosen = {a for a in sys.argv[1:] if a in ("read", "draft", "send")}
    ok = anyio.run(run, chosen or {"read", "draft", "send"})
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
