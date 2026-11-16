"""Minimal terminal chat with the local model, through the model queue.

Run:  python -m model.serving.chat      (type 'exit' or press Ctrl+C to quit)
"""

from __future__ import annotations

import sys

from agents.runtime.model_queue import get_model_queue
from model.serving import Message, load_model_config


def main() -> int:
    for stream in (sys.stdout, sys.stdin):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    config = load_model_config()
    queue = get_model_queue()
    history: list[Message] = [
        Message(role="system", content="You are HCWMA, a helpful private assistant.")
    ]
    print(f"HCWMA chat — {config.ollama_model} via {config.runtime}. Type 'exit' to quit.")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if text.lower() in {"exit", "quit"}:
            return 0
        if not text:
            continue
        history.append(Message(role="user", content=text))
        result = queue.generate(history, max_tokens=512)
        history.append(Message(role="assistant", content=result.text))
        print(f"qwen> {result.text.strip()}")
        print(f"      [{result.completion_tokens} tok, {result.tokens_per_s:.1f} tok/s]")


if __name__ == "__main__":
    raise SystemExit(main())
