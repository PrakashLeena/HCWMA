"""Measure the RAM of Ollama's model process (llama-server) over N requests (backlog item 1).

Sends N short requests that cycle through HCWMA's real agent prompts (`agents/prompts/`),
the way a session switches between the Router and sub-agents, and after each one reads the
working set and private bytes of the llama-server process(es) started by Ollama.

    python -m model.evaluation.ram_soak --runs 20 [--label before] [--out FILE] [--port 11435]

Needs Ollama running on 127.0.0.1:11434 with the model available. It sends prompts to the
local model only; nothing leaves the PC. Takes ~2-5 min on CPU.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from model.serving.config import REPO_ROOT, load_model_config
from model.serving.ollama_backend import OllamaBackend
from model.serving.runner_memory import runner_memory
from model.serving.types import Message

PROMPTS = ["router", "file_system", "planner", "communication", "documents_study"]
REQUESTS = [
    "Where is my CV file?",
    "Remind me on Friday to call the bank.",
    "Summarize the notes from yesterday.",
    "Read my latest WhatsApp messages.",
    "What can you do?",
]


def runner_ram_mb() -> tuple[int, int]:
    mem = runner_memory()
    return mem.working_set_mb, mem.private_mb


def _unload_others(backend: OllamaBackend) -> None:
    """Measure one model alone: unload every other model Ollama holds."""
    for loaded in backend._get_json("/api/ps").get("models", []):
        name = loaded.get("name")
        if name and name != backend.config.ollama_model:
            for _ in backend._post_lines("/api/generate", {"model": name, "keep_alive": 0}):
                pass


def run(runs: int, label: str, port: int | None = None) -> list[tuple[int, int, int, float]]:
    config = load_model_config()
    if port is not None:  # e.g. a second Ollama started for an A/B measurement
        config.endpoint.port = port
    backend = OllamaBackend(config)
    prompts = [
        (REPO_ROOT / "agents" / "prompts" / f"{name}.md").read_text(encoding="utf-8")
        for name in PROMPTS
    ]
    _unload_others(backend)
    rows: list[tuple[int, int, int, float]] = []
    print(f"[{label}] before any request: ws/private MB = {runner_ram_mb()}")
    for i in range(runs):
        system = prompts[i % len(prompts)]
        user = REQUESTS[(i // len(prompts)) % len(REQUESTS)] + f" (request {i + 1})"
        t0 = time.perf_counter()
        backend.generate(
            [Message(role="system", content=system), Message(role="user", content=user)],
            max_tokens=48,
        )
        seconds = time.perf_counter() - t0
        time.sleep(1.0)  # let the server finish saving its prompt cache
        ws, priv = runner_ram_mb()
        rows.append((i + 1, ws, priv, seconds))
        print(f"[{label}] {i + 1:2d}  ws {ws:5d} MB  private {priv:5d} MB  {seconds:5.1f} s")
    return rows


def to_markdown(label: str, rows: list[tuple[int, int, int, float]]) -> str:
    lines = [
        f"### {label}",
        "",
        "| request | working set MB | private MB | seconds |",
        "|---:|---:|---:|---:|",
    ]
    lines += [f"| {n} | {ws} | {priv} | {s:.1f} |" for n, ws, priv, s in rows]
    first, last = rows[0], rows[-1]
    peak = max(r[1] for r in rows)
    lines += [
        "",
        f"After request 1: {first[1]} MB, after request {last[0]}: {last[1]} MB, "
        f"peak {peak} MB (working set).",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--label", default="run")
    parser.add_argument("--port", type=int, help="Ollama port on 127.0.0.1 (default: config)")
    parser.add_argument("--out", type=Path, help="append a markdown table to this file")
    args = parser.parse_args()
    rows = run(args.runs, args.label, args.port)
    text = to_markdown(args.label, rows)
    print(text)
    if args.out:
        with args.out.open("a", encoding="utf-8") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
