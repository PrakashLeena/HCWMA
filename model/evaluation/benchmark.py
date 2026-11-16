"""Benchmark the local model on this PC (build step 3, model-setup skill section 5).

Run:  python -m model.evaluation.benchmark --runs 3 --output docs/benchmarks.md

Measures load time, RAM, time to first token, tokens/s and tool-call JSON validity over a
fixed set of 10 prompts, repeated `--runs` times, and writes a Markdown report.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import psutil

from model.serving import Backend, GenerateResult, Message, ModelConfig, load_model_config
from model.serving.config import REPO_ROOT

# --------------------------------------------------------------------------------------------
# Fixed prompt set
# --------------------------------------------------------------------------------------------

AGENTS_HINT = (
    "Available agents: file_system (files, folders, PC), coding (write or fix code), "
    "image_editing (edit pictures), web_research (search the web), daily_life "
    "(reminders, calendar, shopping). Answer with the agent name only."
)

TOOL_GET_TIME = {
    "type": "function",
    "function": {
        "name": "get_system_time",
        "description": "Get the current local date and time of the PC.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

TOOL_MOVE_FILE = {
    "type": "function",
    "function": {
        "name": "move_file",
        "description": "Move a file to another folder inside an approved folder.",
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Path of the file to move"},
                "destination_folder": {"type": "string", "description": "Target folder"},
            },
            "required": ["source", "destination_folder"],
        },
    },
}

TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "due_date": {"type": "string"},
        "priority": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["title", "due_date", "priority"],
}

NEEDLE = "ORCHID-42"


def _long_context_text() -> str:
    paragraph = (
        "Section {i}. The household notes record routine maintenance: the water filter was "
        "changed, the router was restarted, the garden was watered and the bills were filed. "
        "Nothing unusual happened and every item was marked complete by the evening. "
    )
    parts = [paragraph.format(i=i) for i in range(1, 61)]
    parts.insert(37, f"Important: the backup code word for the home server is {NEEDLE}. ")
    return "".join(parts)


Check = Callable[[GenerateResult], bool]


@dataclass(frozen=True)
class BenchPrompt:
    id: str
    kind: str
    messages: list[Message]
    max_tokens: int = 160
    tools: list[dict[str, Any]] | None = None
    response_format: dict[str, Any] | None = None
    check: Check | None = None
    is_json_task: bool = False


def _tool_check(name: str, required: list[str]) -> Check:
    def check(result: GenerateResult) -> bool:
        return any(
            tc.name == name and all(k in tc.arguments for k in required) for tc in result.tool_calls
        )

    return check


def _json_check(required: list[str]) -> Check:
    def check(result: GenerateResult) -> bool:
        try:
            data = json.loads(result.text)
        except (json.JSONDecodeError, ValueError):
            return False
        return isinstance(data, dict) and all(k in data for k in required)

    return check


def _contains(*needles: str) -> Check:
    def check(result: GenerateResult) -> bool:
        low = result.text.lower()
        return any(n.lower() in low for n in needles)

    return check


def _user(text: str, system: str | None = None) -> list[Message]:
    msgs = [Message(role="system", content=system)] if system else []
    return [*msgs, Message(role="user", content=text)]


PROMPTS: list[BenchPrompt] = [
    BenchPrompt(
        "chat_greeting",
        "chat",
        _user("Hi! In one sentence, what can a private desktop assistant help me with?"),
        max_tokens=80,
    ),
    BenchPrompt(
        "chat_explain",
        "chat",
        _user("Explain in three short bullet points why backups matter."),
        max_tokens=160,
    ),
    BenchPrompt(
        "routing_clear",
        "routing",
        _user("Which agent handles: 'move my invoices into the Taxes folder'?", AGENTS_HINT),
        max_tokens=16,
        check=_contains("file_system"),
    ),
    BenchPrompt(
        "routing_code",
        "routing",
        _user("Which agent handles: 'my Python script crashes with KeyError'?", AGENTS_HINT),
        max_tokens=16,
        check=_contains("coding"),
    ),
    BenchPrompt(
        "tool_no_args",
        "tool_call",
        _user("What time is it on my PC right now? Use a tool."),
        tools=[TOOL_GET_TIME, TOOL_MOVE_FILE],
        max_tokens=128,
        check=_tool_check("get_system_time", []),
        is_json_task=True,
    ),
    BenchPrompt(
        "tool_with_args",
        "tool_call",
        _user("Move the file Documents/report.pdf into the folder Documents/Archive."),
        tools=[TOOL_GET_TIME, TOOL_MOVE_FILE],
        max_tokens=128,
        check=_tool_check("move_file", ["source", "destination_folder"]),
        is_json_task=True,
    ),
    BenchPrompt(
        "json_schema",
        "json_schema",
        _user(
            "Create a task as JSON: pay the electricity bill by 2026-11-30, high priority. "
            "Fields: title, due_date, priority."
        ),
        response_format=TASK_SCHEMA,
        max_tokens=96,
        check=_json_check(["title", "due_date", "priority"]),
        is_json_task=True,
    ),
    BenchPrompt(
        "long_context",
        "long_context",
        _user(_long_context_text() + "\n\nQuestion: what is the backup code word? Reply briefly."),
        max_tokens=32,
        check=_contains(NEEDLE),
    ),
    BenchPrompt(
        "tamil",
        "tamil",
        _user("வணக்கம்! நல்ல தூக்கத்திற்கு இரண்டு எளிய குறிப்புகளை தமிழில் சுருக்கமாக சொல்லுங்கள்."),
        max_tokens=160,
    ),
    BenchPrompt(
        "sinhala",
        "sinhala",
        _user("ආයුබෝවන්! හොඳ නින්දක් සඳහා සරල උපදෙස් දෙකක් සිංහලෙන් කෙටියෙන් දෙන්න."),
        max_tokens=160,
    ),
]


# --------------------------------------------------------------------------------------------
# Memory sampling
# --------------------------------------------------------------------------------------------


def runtime_rss_bytes(name_prefix: str = "ollama") -> int:
    """Resident memory of the runtime: every `ollama*` process plus all its children.

    On Windows the model itself runs in a child process (`llama-server.exe`).
    """
    seen: set[int] = set()
    total = 0
    for proc in psutil.process_iter(["name"]):
        try:
            if not (proc.info.get("name") or "").lower().startswith(name_prefix):
                continue
            for p in [proc, *proc.children(recursive=True)]:
                if p.pid not in seen:
                    seen.add(p.pid)
                    total += p.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return total


class PeakSampler:
    """Samples a reading in a background thread and keeps the extreme (max by default)."""

    def __init__(
        self,
        read: Callable[[], int],
        interval_s: float = 0.25,
        pick: Callable[[int, int], int] = max,
    ) -> None:
        self._read = read
        self._interval = interval_s
        self._pick = pick
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self.peak = 0

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.peak = self._pick(self.peak, self._read())
            self._stop.wait(self._interval)

    def __enter__(self) -> PeakSampler:
        self.peak = self._read()
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()
        self.peak = self._pick(self.peak, self._read())


# --------------------------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------------------------


@dataclass
class PromptRun:
    id: str
    kind: str
    ttft_s: float | None
    total_s: float
    prompt_tokens: int
    completion_tokens: int
    tokens_per_s: float
    prompt_tokens_per_s: float
    ok: bool | None
    is_json_task: bool
    preview: str


@dataclass
class RunResult:
    load_s: float
    ram_before_mb: float
    ram_loaded_mb: float
    ram_peak_mb: float
    system_ram_free_min_mb: float
    prompts: list[PromptRun] = field(default_factory=list)

    @property
    def json_valid(self) -> tuple[int, int]:
        tasks = [p for p in self.prompts if p.is_json_task]
        return sum(1 for p in tasks if p.ok), len(tasks)


def _mb(n: float) -> float:
    return n / (1024 * 1024)


def run_prompt(backend: Backend, prompt: BenchPrompt) -> PromptRun:
    try:
        result = backend.generate(
            prompt.messages,
            tools=prompt.tools,
            max_tokens=prompt.max_tokens,
            response_format=prompt.response_format,
        )
    except ValueError:
        # e.g. tool-call arguments that are not valid JSON
        return PromptRun(prompt.id, prompt.kind, None, 0.0, 0, 0, 0.0, 0.0, False, True, "")
    ok = prompt.check(result) if prompt.check else None
    preview = result.text.strip().replace("\n", " ")
    if result.tool_calls:
        preview = "; ".join(f"{tc.name}({json.dumps(tc.arguments)})" for tc in result.tool_calls)
    return PromptRun(
        id=prompt.id,
        kind=prompt.kind,
        ttft_s=result.ttft_s,
        total_s=result.total_s,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        tokens_per_s=result.tokens_per_s,
        prompt_tokens_per_s=result.prompt_tokens_per_s,
        ok=ok,
        is_json_task=prompt.is_json_task,
        preview=preview[:80],
    )


def run_once(
    backend: Backend,
    prompts: list[BenchPrompt] = PROMPTS,
    read_rss: Callable[[], int] = runtime_rss_bytes,
    read_system_free: Callable[[], int] = lambda: psutil.virtual_memory().available,
    log: Callable[[str], None] = lambda s: None,
) -> RunResult:
    backend.unload()
    time.sleep(1.0)
    before = read_rss()
    with (
        PeakSampler(read_rss) as rss_peak,
        PeakSampler(read_system_free, pick=min) as free_min,
    ):
        load_s = backend.load()
        loaded = read_rss()
        runs = []
        for p in prompts:
            log(f"  {p.id} ...")
            runs.append(run_prompt(backend, p))
    return RunResult(
        load_s=load_s,
        ram_before_mb=_mb(before),
        ram_loaded_mb=_mb(loaded),
        ram_peak_mb=_mb(rss_peak.peak),
        system_ram_free_min_mb=_mb(free_min.peak),
        prompts=runs,
    )


# --------------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------------


def spread(values: list[float]) -> str:
    vals = [v for v in values if v is not None]
    if not vals:
        return "n/a"
    lo, med, hi = min(vals), statistics.median(vals), max(vals)
    return f"{med:.2f} ({lo:.2f}–{hi:.2f})"


def hardware_summary() -> dict[str, str]:
    vm = psutil.virtual_memory()
    return {
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "cpu": platform.processor() or platform.machine(),
        "cores": f"{psutil.cpu_count(logical=False)} cores / {psutil.cpu_count()} threads",
        "ram": f"{vm.total / 1024**3:.1f} GB",
        "python": platform.python_version(),
    }


def render_markdown(
    runs: list[RunResult],
    config: ModelConfig,
    runtime: dict[str, Any],
    hardware: dict[str, str],
    today: date | None = None,
) -> str:
    today = today or date.today()
    n = len(runs)
    lines = [
        "# HCWMA model benchmarks",
        "",
        "Generated by `python -m model.evaluation.benchmark`. Re-run after any change to",
        "`configs/model.yaml`, the runtime version or the PC.",
        "",
        f"## {config.ollama_model} ({config.quantization}, {config.device}) — {today.isoformat()}",
        "",
        "| Setting | Value |",
        "|---|---|",
        f"| Checkpoint | {config.checkpoint} |",
        f"| Runtime | {runtime.get('runtime')} {runtime.get('version')} |",
        f"| Context length | {config.context_length} |",
        f"| Thinking mode | {'on' if config.think else 'off'} |",
        f"| Runs | {n} |",
    ]
    for key, value in hardware.items():
        lines.append(f"| {key.upper() if key in ('os', 'cpu', 'ram') else key.title()} | {value} |")

    lines += [
        "",
        "### Summary (median, with min–max over runs)",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Load time (s) | {spread([r.load_s for r in runs])} |",
        f"| Runtime RAM idle before load (MB) | {spread([r.ram_before_mb for r in runs])} |",
        f"| Runtime RAM after load (MB) | {spread([r.ram_loaded_mb for r in runs])} |",
        f"| Runtime RAM peak (MB) | {spread([r.ram_peak_mb for r in runs])} |",
        f"| Free system RAM, lowest during run (MB) | "
        f"{spread([r.system_ram_free_min_mb for r in runs])} |",
    ]
    all_prompts = [p for r in runs for p in r.prompts]
    gen = [p.tokens_per_s for p in all_prompts if p.completion_tokens > 0]
    lines.append(f"| Generation speed, all prompts (tok/s) | {spread(gen)} |")
    short = [p.ttft_s for p in all_prompts if p.kind != "long_context" and p.ttft_s is not None]
    lines.append(f"| Time to first token, short prompts (s) | {spread(short)} |")
    valid = sum(r.json_valid[0] for r in runs)
    total = sum(r.json_valid[1] for r in runs)
    lines.append(f"| Tool-call / JSON validity | {valid}/{total} |")
    lines.append(
        "| Valid JSON per run | "
        + ", ".join(f"{a}/{b}" for a, b in (r.json_valid for r in runs))
        + " |"
    )

    lines += [
        "",
        "### Per prompt (median, with min–max over runs)",
        "",
        "| Prompt | Kind | Prompt tok | TTFT (s) | Prefill tok/s | Gen tok/s | Total (s) | Pass |",
        "|---|---|---|---|---|---|---|---|",
    ]
    ids = [p.id for p in runs[0].prompts] if runs else []
    for pid in ids:
        rows = [p for r in runs for p in r.prompts if p.id == pid]
        kind = rows[0].kind
        checks = [p.ok for p in rows if p.ok is not None]
        passed = f"{sum(checks)}/{len(checks)}" if checks else "—"
        lines.append(
            f"| {pid} | {kind} | {rows[0].prompt_tokens} | {spread([p.ttft_s for p in rows])} | "
            f"{spread([p.prompt_tokens_per_s for p in rows])} | "
            f"{spread([p.tokens_per_s for p in rows])} | {spread([p.total_s for p in rows])} | "
            f"{passed} |"
        )

    if runs:
        lines += ["", "### Sample outputs (last run)", "", "| Prompt | Output (first 80 chars) |"]
        lines.append("|---|---|")
        for p in runs[-1].prompts:
            text = p.preview.replace("|", "\\|") or "(empty)"
            lines.append(f"| {p.id} | {text} |")

    lines += [
        "",
        "### Notes",
        "",
        "- Runtime RAM = resident memory of the `ollama*` processes and their children (the",
        "  model runs in a child `llama-server` process). Free system RAM depends on what",
        "  else is open on the PC during the run.",
        "- TTFT includes prompt processing; the long-context prompt is reported separately.",
        "- Tool-call validity counts native tool calls with the expected name and required",
        "  arguments, plus schema-constrained JSON that parses with the required keys.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "docs" / "benchmarks.md")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    from model.serving import make_backend

    config = load_model_config()
    backend = make_backend(config)
    runtime = backend.runtime_info()
    results: list[RunResult] = []
    for i in range(args.runs):
        print(f"run {i + 1}/{args.runs}", flush=True)
        result = run_once(backend, log=lambda s: print(s, flush=True))
        a, b = result.json_valid
        print(f"  load {result.load_s:.1f}s, peak RSS {result.ram_peak_mb:.0f} MB, JSON {a}/{b}")
        results.append(result)
    runtime = {**runtime, **{k: v for k, v in backend.runtime_info().items() if k != "loaded"}}
    report = render_markdown(results, config, runtime, hardware_summary())
    args.output.write_text(report, encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
