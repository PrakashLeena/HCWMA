"""How big is a sub-agent's first prompt? Parts in characters, total in model tokens.

    python -m agents.router.measure_agent_prompt            # file_system, "find amma folder"
    python -m agents.router.measure_agent_prompt --agent file_system --message "..."
    python -m agents.router.measure_agent_prompt --tokens   # also count tokens with the model
                                                            # (one call, ~1 min on CPU)

The agent is delegated to directly, as the Router would, with a model that only records what
it is sent: the parts are exact and the run takes seconds unless --tokens is given. The real
tools, card, prompt and memory are used; nothing is written to memory, and audit records go to
a temporary file, not the real audit log.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

import anyio

from agents.router.factory import open_router
from agents.runtime.model_queue import ModelQueue, get_model_queue
from agents.runtime.task_envelope import TaskContext
from model.serving import GenerateResult, Message
from security.audit.audit_logger import AuditLogger

MEMORY_HEADER = "\n\n<memory"  # where the memory block starts in the request


def parts(messages: list[Message], tools: list[dict[str, Any]] | None) -> dict[str, int]:
    """Character counts of the pieces a sub-agent's first model call is made of."""
    system = messages[0].content
    request = messages[1].content
    memory = request[request.index(MEMORY_HEADER) :] if MEMORY_HEADER in request else ""
    return {
        "system prompt (card prompt + runtime rules)": len(system),
        "tool schemas (JSON)": len(json.dumps(tools or [])),
        "request": len(request) - len(memory),
        "memory block": len(memory),
    }


async def capture(agent: str, message: str) -> tuple[list[Message], list[dict[str, Any]] | None]:
    """Delegate straight to `agent` as the Router would (same context and grant), with a
    model that only records what it is sent. Nothing is written to memory."""
    seen: dict[str, Any] = {}

    def fake(messages: list[Message], tools: Any = None, **_: Any) -> GenerateResult:
        seen.setdefault("messages", messages)
        seen.setdefault("tools", tools)
        return GenerateResult(text="(captured)")

    with tempfile.TemporaryDirectory() as tmp:
        audit = AuditLogger(Path(tmp) / "audit.jsonl")
        async with open_router(queue=ModelQueue(fake), audit=audit) as router:
            runtime = router._runtime  # noqa: SLF001 - a measuring script, not app code
            await runtime.delegate(
                from_agent="router",
                to_agent=agent,
                goal=message,
                budget=runtime.new_budget(),
                root_task_id="measure",
                parent_task_id="measure",
                depth=2,
                context=TaskContext(user_message=message),
                read_only=False,
            )
    return seen["messages"], seen["tools"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agent", default="file_system")
    parser.add_argument("--message", default="find amma folder")
    parser.add_argument("--tokens", action="store_true", help="count tokens with the real model")
    args = parser.parse_args(argv)

    messages, tools = anyio.run(capture, args.agent, args.message)
    sizes = parts(messages, tools)
    print(f"{args.agent} first call for {args.message!r}:")
    for name, chars in sizes.items():
        print(f"  {name:<45} {chars:6d} chars")
    print(f"  {'total':<45} {sum(sizes.values()):6d} chars")
    print(f"  tools offered: {', '.join(t['function']['name'] for t in tools or []) or 'none'}")
    if args.tokens:
        result = get_model_queue().generate(messages, tools=tools, max_tokens=1)
        print(
            f"  prompt tokens (model): {result.prompt_tokens}  "
            f"(prefill {result.prompt_eval_s:.1f} s)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
