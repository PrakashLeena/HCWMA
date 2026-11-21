"""Measure Router prompt size and decision latency on a few requests:

python -m agents.router.measure_speed
"""

from __future__ import annotations

import time

import anyio
import httpx

from agents.registry.agent_registry import AgentRegistry
from agents.router.classifier import Classifier
from agents.router.eval_routing import _NoGateway, outcome
from agents.runtime.agent_runtime import AgentRuntime
from agents.runtime.model_queue import get_model_queue
from model.serving import Message, load_model_config
from security.audit.audit_logger import get_audit_logger
from security.permissions.permission_manager import load_permissions_config

REQUESTS = [
    "find my lost report",
    "resize all the photos in my Pictures folder to 1080 pixels wide",
    "my function returns None instead of the sum",
]
OLLAMA = "http://127.0.0.1:11434/api/chat"
MODEL = "qwen3.5:4b"
CONTEXT_LENGTH = load_model_config().context_length


def prompt_tokens(system: str, user: str) -> int:
    body = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False,
        "think": False,
        # Same num_ctx as HCWMA's requests: a different one would reload the model.
        "options": {"num_predict": 1, "temperature": 0, "num_ctx": CONTEXT_LENGTH},
    }
    r = httpx.post(OLLAMA, json=body, timeout=600)
    return int(r.json()["prompt_eval_count"])


async def main() -> None:
    registry = AgentRegistry.from_dir()
    runtime = AgentRuntime(
        registry=registry,
        queue=get_model_queue(),
        gateway=_NoGateway(),  # type: ignore[arg-type]
        audit=get_audit_logger(),
        limits=load_permissions_config().limits,
    )
    clf = Classifier(registry, runtime)
    system = clf.system_prompt()
    print(f"system prompt: {len(system)} chars, {len(system.split())} words")
    for text in REQUESTS:
        tokens = prompt_tokens(system, text)
        start = time.perf_counter()
        d = await clf.classify([Message(role="user", content=text)])
        print(
            f"{time.perf_counter() - start:5.1f}s  prompt_tokens={tokens}  -> {outcome(d)} {text!r}"
        )


if __name__ == "__main__":
    anyio.run(main)
