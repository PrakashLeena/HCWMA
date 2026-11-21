"""Backlog item 1: which processes count as Ollama's model runner."""

from model.serving.runner_memory import _is_runner, runner_memory


def test_runner_names() -> None:
    assert _is_runner("llama-server.exe", [])
    assert _is_runner("ollama_llama_server", [])
    assert _is_runner("ollama.exe", ["ollama.exe", "runner", "--model", "x"])
    assert not _is_runner("ollama.exe", ["ollama.exe", "serve"])
    assert not _is_runner("python.exe", ["python", "-m", "llama-server"])


def test_runner_memory_never_raises() -> None:
    mem = runner_memory()
    assert mem.working_set_mb >= 0 and mem.processes >= 0
