"""Step 3: the Ollama wrapper builds the right request and parses streamed replies.

The model is mocked: these tests never contact Ollama.
"""

import json
import urllib.request
from typing import Any

import pytest

from model.serving import Message, load_model_config
from model.serving.ollama_backend import OllamaBackend


class FakeResponse:
    def __init__(self, lines: list[dict[str, Any]]) -> None:
        self._lines = [json.dumps(x).encode() + b"\n" for x in lines]

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._lines)

    def read(self) -> bytes:
        return b"".join(self._lines)


class FakeOpener:
    def __init__(self, lines: list[dict[str, Any]]) -> None:
        self.lines = lines
        self.requests: list[urllib.request.Request] = []

    def __call__(self, req: urllib.request.Request, timeout: float) -> FakeResponse:
        self.requests.append(req)
        return FakeResponse(self.lines)

    def payload(self, i: int = -1) -> dict[str, Any]:
        data = self.requests[i].data
        assert isinstance(data, bytes)
        return json.loads(data)


FINAL = {
    "done": True,
    "message": {"role": "assistant", "content": ""},
    "prompt_eval_count": 20,
    "prompt_eval_duration": 2_000_000_000,
    "eval_count": 10,
    "eval_duration": 1_000_000_000,
    "load_duration": 500_000_000,
}


def _backend(lines: list[dict[str, Any]]) -> tuple[OllamaBackend, FakeOpener]:
    opener = FakeOpener(lines)
    return OllamaBackend(load_model_config(), opener=opener), opener


def test_generate_request_is_local_and_well_formed() -> None:
    backend, opener = _backend([FINAL])
    tool = {"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}
    backend.generate([Message(role="user", content="hi")], tools=[tool], max_tokens=42)

    req = opener.requests[0]
    assert req.full_url == "http://127.0.0.1:11434/api/chat"
    body = opener.payload()
    assert body["model"] == "qwen3.5:4b"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["options"]["num_predict"] == 42
    assert body["options"]["num_ctx"] == 8192
    assert body["tools"] == [tool]
    assert body["think"] is False
    assert body["stream"] is True
    assert "format" not in body


def test_generate_passes_json_schema_as_format() -> None:
    backend, opener = _backend([FINAL])
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    backend.generate([Message(role="user", content="x")], response_format=schema)
    assert opener.payload()["format"] == schema
    assert "tools" not in opener.payload()


def test_streamed_text_and_metrics_are_parsed() -> None:
    backend, _ = _backend(
        [
            {"done": False, "message": {"role": "assistant", "content": "Hel"}},
            {"done": False, "message": {"role": "assistant", "content": "lo"}},
            FINAL,
        ]
    )
    result = backend.generate([Message(role="user", content="hi")])
    assert result.text == "Hello"
    assert result.tool_calls == []
    assert result.prompt_tokens == 20
    assert result.completion_tokens == 10
    assert result.tokens_per_s == pytest.approx(10.0)
    assert result.prompt_tokens_per_s == pytest.approx(10.0)
    assert result.load_s == pytest.approx(0.5)
    assert result.ttft_s is not None


def test_tool_calls_are_proposals_with_dict_arguments() -> None:
    call = {"function": {"name": "move_file", "arguments": {"source": "a", "destination": "b"}}}
    as_string = {"function": {"name": "get_time", "arguments": "{}"}}
    backend, _ = _backend(
        [{"done": False, "message": {"role": "assistant", "tool_calls": [call, as_string]}}, FINAL]
    )
    result = backend.generate([Message(role="user", content="move it")])
    assert [tc.name for tc in result.tool_calls] == ["move_file", "get_time"]
    assert result.tool_calls[0].arguments == {"source": "a", "destination": "b"}
    assert result.tool_calls[1].arguments == {}


def test_invalid_tool_arguments_raise() -> None:
    bad = {"function": {"name": "x", "arguments": "{not json"}}
    backend, _ = _backend([{"done": False, "message": {"tool_calls": [bad]}}, FINAL])
    with pytest.raises(ValueError):
        backend.generate([Message(role="user", content="x")])


def test_runtime_error_is_raised() -> None:
    backend, _ = _backend([{"error": "model not found"}])
    with pytest.raises(RuntimeError, match="model not found"):
        backend.generate([Message(role="user", content="x")])


def test_unload_sends_keep_alive_zero() -> None:
    backend, opener = _backend([{"done": True}])
    backend.unload()
    assert opener.requests[0].full_url.endswith("/api/generate")
    assert opener.payload() == {"model": "qwen3.5:4b", "keep_alive": 0}


def test_non_local_endpoint_refused() -> None:
    config = load_model_config()
    # Bypass validation to simulate a tampered config object.
    tampered = config.model_copy(
        update={"endpoint": config.endpoint.model_copy(update={"host": "10.0.0.2"})}
    )
    with pytest.raises(ValueError, match="non-local"):
        OllamaBackend(tampered, opener=FakeOpener([]))
