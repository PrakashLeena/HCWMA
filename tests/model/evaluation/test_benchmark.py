"""Step 3: benchmark prompt set and report, with the model mocked."""

from collections.abc import Iterator
from datetime import date
from typing import Any

from model.evaluation import benchmark as bm
from model.serving import GenerateResult, Message, ToolCall, load_model_config


def test_prompt_set_covers_the_required_cases() -> None:
    assert len(bm.PROMPTS) == 10
    kinds = {p.kind for p in bm.PROMPTS}
    assert {"chat", "routing", "tool_call", "json_schema", "long_context"} <= kinds
    assert {"tamil", "sinhala"} <= kinds
    assert len({p.id for p in bm.PROMPTS}) == 10
    long = next(p for p in bm.PROMPTS if p.kind == "long_context")
    assert len(long.messages[-1].content) > 8000
    assert bm.NEEDLE in long.messages[-1].content


def test_checks() -> None:
    tool_ok = GenerateResult(
        text="",
        tool_calls=[
            ToolCall(name="move_file", arguments={"source": "a", "destination_folder": "b"})
        ],
    )
    tool_bad = GenerateResult(text='{"name": "move_file"}')
    check = bm._tool_check("move_file", ["source", "destination_folder"])
    assert check(tool_ok) and not check(tool_bad)

    jcheck = bm._json_check(["title"])
    assert jcheck(GenerateResult(text='{"title": "x"}'))
    assert not jcheck(GenerateResult(text="not json"))
    assert not jcheck(GenerateResult(text='{"other": 1}'))


class FakeBackend:
    """Answers every benchmark prompt correctly, without a model."""

    def __init__(self) -> None:
        self.loads = 0
        self.unloads = 0

    def load(self) -> float:
        self.loads += 1
        return 1.5

    def unload(self) -> None:
        self.unloads += 1

    def runtime_info(self) -> dict[str, Any]:
        return {"runtime": "fake", "version": "0"}

    def generate(
        self, messages: list[Message], tools: Any = None, max_tokens: int = 0, **kw: Any
    ) -> GenerateResult:
        text = messages[-1].content
        common = dict(prompt_tokens=10, completion_tokens=5, eval_s=0.5, prompt_eval_s=0.1)
        if tools and "Move" in text:
            call = ToolCall(name="move_file", arguments={"source": "a", "destination_folder": "b"})
            return GenerateResult(text="", tool_calls=[call], ttft_s=0.2, **common)
        if tools:
            return GenerateResult(
                text="", tool_calls=[ToolCall(name="get_system_time")], ttft_s=0.2, **common
            )
        if kw.get("response_format"):
            return GenerateResult(
                text='{"title":"t","due_date":"d","priority":"high"}', ttft_s=0.2, **common
            )
        if bm.NEEDLE in text:
            return GenerateResult(text=bm.NEEDLE, ttft_s=3.0, **common)
        if "invoices" in text:
            return GenerateResult(text="file_system", ttft_s=0.2, **common)
        if "KeyError" in text:
            return GenerateResult(text="coding", ttft_s=0.2, **common)
        return GenerateResult(text="hello | world", ttft_s=0.2, **common)


def _rss() -> Iterator[int]:
    n = 100
    while True:
        n += 50
        yield n * 1024 * 1024


def test_run_once_and_report(monkeypatch: Any) -> None:
    monkeypatch.setattr(bm.time, "sleep", lambda s: None)
    backend = FakeBackend()
    gen = _rss()
    runs = [
        bm.run_once(backend, read_rss=lambda: next(gen), read_system_free=lambda: 4 * 1024**3)
        for _ in range(3)
    ]
    assert backend.loads == 3 and backend.unloads == 3
    for run in runs:
        assert run.json_valid == (3, 3)
        assert all(p.ok is not False for p in run.prompts)
        assert run.ram_peak_mb >= run.ram_loaded_mb >= run.ram_before_mb
        assert run.system_ram_free_min_mb == 4096

    report = bm.render_markdown(
        runs,
        load_model_config(),
        backend.runtime_info(),
        {"cpu": "test cpu", "ram": "16 GB"},
        today=date(2026, 11, 16),
    )
    assert "qwen3.5-text:4b" in report
    assert "| Tool-call / JSON validity | 9/9 |" in report
    assert "| Valid JSON per run | 3/3, 3/3, 3/3 |" in report
    assert "| Load time (s) | 1.50 (1.50–1.50) |" in report
    for p in bm.PROMPTS:
        assert f"| {p.id} |" in report
    assert "hello \\| world" in report  # pipes escaped in the table


def test_invalid_tool_json_counts_as_failure() -> None:
    class Broken(FakeBackend):
        def generate(self, *a: Any, **kw: Any) -> GenerateResult:
            raise ValueError("bad tool json")

    prompt = next(p for p in bm.PROMPTS if p.kind == "tool_call")
    run = bm.run_prompt(Broken(), prompt)
    assert run.ok is False and run.is_json_task


def test_spread() -> None:
    assert bm.spread([1.0, 3.0, 2.0]) == "2.00 (1.00–3.00)"
    assert bm.spread([]) == "n/a"


def test_runtime_rss_includes_child_processes(monkeypatch: Any) -> None:
    class Mem:
        def __init__(self, rss: int) -> None:
            self.rss = rss

    class Proc:
        def __init__(self, pid: int, name: str, rss: int, kids: list["Proc"]) -> None:
            self.pid, self.info, self._rss, self._kids = pid, {"name": name}, rss, kids

        def children(self, recursive: bool = False) -> list["Proc"]:
            return self._kids

        def memory_info(self) -> Mem:
            return Mem(self._rss)

    runner = Proc(3, "llama-server.exe", 3000, [])
    server = Proc(2, "ollama.exe", 40, [runner])
    procs = [Proc(1, "ollama app.exe", 30, []), server, runner, Proc(4, "chrome.exe", 999, [])]
    monkeypatch.setattr(bm.psutil, "process_iter", lambda attrs: iter(procs))
    assert bm.runtime_rss_bytes() == 3070


def test_peak_sampler_min() -> None:
    values = iter([5, 3, 4, 2, 6] + [6] * 1000)
    with bm.PeakSampler(lambda: next(values), interval_s=0.001, pick=min) as s:
        import time as _t

        _t.sleep(0.05)
    assert s.peak == 2
