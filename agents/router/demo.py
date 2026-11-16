"""Step 7 "Done when" check, run by hand (needs Ollama running):

    python -m agents.router.demo

1. "echo hello HCWMA"     -> real Router (Qwen) -> echo agent (Qwen) -> back with a result.
2. "What time is it?"     -> Router -> echo -> get_system_time over MCP -> back.
3. "do the thing with it" -> the Router asks a clarifying question.
4. A looping task: a scripted stand-in model (Qwen cannot be made to loop on demand) asks
   for a tool on every turn, forever -> stopped by the 20-tool-call limit. Real MCP server,
   policy layer, limits and audit log.

Audit records written are summarised at the end (`security/audit/logs/audit.jsonl`).
"""

from __future__ import annotations

from typing import Any

import anyio

from agents.registry.agent_registry import AgentRegistry
from agents.registry.card import CardLimits
from agents.router.factory import open_router
from agents.router.router import Router, RouterReply
from agents.runtime.model_queue import ModelQueue
from model.serving import GenerateResult, Message, ToolCall
from security.audit.audit_logger import get_audit_logger


async def ask(router: Router, text: str) -> RouterReply:
    print(f"\n> {text}")
    reply = await router.handle([Message(role="user", content=text)])
    path = " -> ".join(["router", *reply.agents])
    stopped = f" | stopped by {reply.stopped_by}" if reply.stopped_by else ""
    print(f"  [{path} | {reply.action} | {reply.status} | tool calls {reply.tool_calls}{stopped}]")
    print(f"  {reply.reply}")
    return reply


class LoopingModel:
    """Routes to `looper`, then asks for get_system_time on every turn, forever."""

    def generate(self, messages: list[Message], **kwargs: Any) -> GenerateResult:
        if kwargs.get("response_format") is not None:
            return GenerateResult(
                text='{"action":"delegate","confidence":1,'
                '"steps":[{"agent":"looper","goal":"tell the time, again and again"}]}'
            )
        return GenerateResult(text="", tool_calls=[ToolCall(name="get_system_time")])


def loop_registry() -> AgentRegistry:
    """Shipped router + an in-memory copy of echo named `looper` with a 50-step limit, so
    the shared tool-call budget (20) is the limit that stops it."""
    shipped = AgentRegistry.from_dir()
    echo = shipped.get("echo")
    if echo is None:
        raise RuntimeError("the echo card is missing")
    looper = echo.model_copy(update={"id": "looper", "limits": CardLimits(max_steps=50)})
    return AgentRegistry([shipped.router, looper], agents_dir=shipped.agents_dir)


async def run() -> bool:
    audit = get_audit_logger()
    before = len(audit.read_all())

    async with open_router() as router:
        echoed = await ask(router, "echo hello HCWMA")
        timed = await ask(router, "What time is it?")
        unclear = await ask(router, "do the thing with it")

    print("\n--- looping task (scripted stand-in model) ---")
    queue = ModelQueue(LoopingModel().generate)
    async with open_router(queue=queue, registry=loop_registry()) as loop_router:
        looped = await ask(loop_router, "tell me the time forever")

    new = audit.read_all()[before:]
    print(f"\naudit records written to {audit.path}: {len(new)}")
    for rec in new:
        if rec.event == "delegation":
            print("  ", rec.model_dump_json(exclude_none=True))
    tool_records = [r for r in new if r.event == "tool_call"]
    print(f"   ... plus {len(tool_records)} tool_call records, e.g.")
    if tool_records:
        print("  ", tool_records[0].model_dump_json(exclude_none=True))

    checks = {
        "router -> echo -> back with a result": echoed.agents == ["echo"]
        and echoed.status == "done",
        "echo used get_system_time over MCP": timed.agents == ["echo"] and timed.tool_calls >= 1,
        "unclear request -> clarifying question": unclear.action == "clarify",
        "looping task stopped by the limits": looped.stopped_by == "max_tool_calls_per_task"
        and looped.tool_calls == 20,
    }
    print()
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    passed = all(checks.values())
    print("\nSTEP 7 DONE-WHEN:", "PASS" if passed else "FAIL")
    return passed


def main() -> None:
    raise SystemExit(0 if anyio.run(run) else 1)


if __name__ == "__main__":
    main()
