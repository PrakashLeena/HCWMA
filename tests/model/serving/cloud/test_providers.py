"""Cloud backends against a fake provider on 127.0.0.1: request shape, tool calls, errors."""

from __future__ import annotations

import json
from typing import Any

import pytest

from model.serving.cloud.providers import (
    AnthropicBackend,
    CloudError,
    GeminiBackend,
    OpenAICompatBackend,
    extract_json,
)
from model.serving.types import Message, ToolCall
from tests.model.serving.cloud.fake_server import fake_provider, openai_reply

KEY = "test-key-not-real-0001"  # noqa: S105
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Find files",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "limit": {"type": ["integer", "null"]}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }
]
CONVERSATION = [
    Message(role="system", content="You are a file agent."),
    Message(role="user", content="find notes"),
    Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall(name="search_files", arguments={"query": "notes"})],
    ),
    Message(role="tool", content="notes.txt", tool_name="search_files"),
]


def test_openai_compat_request_and_tool_call() -> None:
    with fake_provider() as fake:
        fake.reply(openai_reply(calls=[("search_files", {"query": "notes"})], usage=(120, 7)))
        backend = OpenAICompatBackend(
            base_url=fake.url + "/v1", model="m-1", api_key=KEY, thinking_off=True
        )
        result = backend.generate(CONVERSATION, tools=TOOLS, max_tokens=64, temperature=0)
    req = fake.last
    assert req["path"] == "/v1/chat/completions"
    assert req["headers"]["authorization"] == f"Bearer {KEY}"
    body = req["json"]
    assert body["model"] == "m-1" and body["max_tokens"] == 64 and body["temperature"] == 0
    assert body["tools"][0]["function"]["name"] == "search_files"
    # the tool result answers the assistant's call by the same (made-up) id
    call_id = body["messages"][2]["tool_calls"][0]["id"]
    assert body["messages"][3] == {"role": "tool", "tool_call_id": call_id, "content": "notes.txt"}
    assert result.tool_calls == [ToolCall(name="search_files", arguments={"query": "notes"})]
    assert (result.prompt_tokens, result.completion_tokens) == (120, 7)
    assert (result.provider, result.model, result.is_cloud) == ("openai_compat", "m-1", True)


def test_openai_compat_json_mode_puts_the_schema_in_the_system_prompt() -> None:
    schema = {"type": "object", "properties": {"action": {"type": "string"}}}
    with fake_provider() as fake:
        fake.reply(openai_reply(text='```json\n{"action": "answer"}\n```'))
        backend = OpenAICompatBackend(base_url=fake.url, model="m", api_key=KEY)
        result = backend.generate(
            [Message(role="system", content="Route."), Message(role="user", content="hi")],
            response_format=schema,
        )
    body = fake.last["json"]
    assert body["response_format"] == {"type": "json_object"}
    assert '"action"' in body["messages"][0]["content"]
    assert json.loads(result.text) == {"action": "answer"}


def test_tool_arguments_that_are_not_json_are_refused() -> None:
    with fake_provider() as fake:
        fake.reply(openai_reply(calls=[("search_files", "{not json")]))
        backend = OpenAICompatBackend(base_url=fake.url, model="m", api_key=KEY)
        with pytest.raises(CloudError) as err:
            backend.generate([Message(role="user", content="x")], tools=TOOLS)
    assert err.value.kind == "invalid"


@pytest.mark.parametrize(
    ("status", "kind"), [(429, "rate_limit"), (401, "auth"), (503, "server"), (400, "bad_request")]
)
def test_http_errors_have_a_kind_and_never_the_key(status: int, kind: str) -> None:
    with fake_provider() as fake:
        fake.reply({"error": {"message": f"bad key {KEY}"}}, status=status)
        backend = OpenAICompatBackend(base_url=fake.url, model="m", api_key=KEY)
        with pytest.raises(CloudError) as err:
            backend.generate([Message(role="user", content="x")])
    assert err.value.kind == kind
    assert KEY not in str(err.value)
    assert KEY not in repr(backend)


def test_unreachable_provider_is_a_network_error() -> None:
    backend = OpenAICompatBackend(
        base_url="http://127.0.0.1:9", model="m", api_key=KEY, timeout_s=2
    )
    with pytest.raises(CloudError) as err:
        backend.generate([Message(role="user", content="x")])
    assert err.value.kind in {"network", "timeout"}


def test_anthropic_request_and_tool_use() -> None:
    with fake_provider() as fake:
        fake.reply(
            {
                "content": [
                    {"type": "text", "text": "Looking."},
                    {
                        "type": "tool_use",
                        "id": "t",
                        "name": "search_files",
                        "input": {"query": "a"},
                    },
                ],
                "usage": {"input_tokens": 50, "output_tokens": 9},
            }
        )
        backend = AnthropicBackend(base_url=fake.url, model="claude-x", api_key=KEY)
        result = backend.generate(CONVERSATION, tools=TOOLS)
    req = fake.last
    assert req["path"] == "/v1/messages"
    assert req["headers"]["x-api-key"] == KEY
    body = req["json"]
    assert body["system"] == "You are a file agent."
    assert body["tools"][0]["input_schema"]["required"] == ["query"]
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "user"]
    tool_use = body["messages"][1]["content"][0]
    tool_result = body["messages"][2]["content"][0]
    assert tool_result == {
        "type": "tool_result",
        "tool_use_id": tool_use["id"],
        "content": "notes.txt",
    }
    assert result.text == "Looking."
    assert result.tool_calls == [ToolCall(name="search_files", arguments={"query": "a"})]
    assert (result.prompt_tokens, result.completion_tokens) == (50, 9)


def test_gemini_request_and_function_call() -> None:
    with fake_provider() as fake:
        fake.reply(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"functionCall": {"name": "search_files", "args": {"query": "b"}}}
                            ]
                        }
                    }
                ],
                "usageMetadata": {"promptTokenCount": 30, "candidatesTokenCount": 4},
            }
        )
        backend = GeminiBackend(base_url=fake.url, model="gemini-x", api_key=KEY)
        result = backend.generate(CONVERSATION, tools=TOOLS)
    req = fake.last
    assert req["path"] == "/v1beta/models/gemini-x:generateContent"
    assert req["headers"]["x-goog-api-key"] == KEY
    assert KEY not in req["path"]
    body = req["json"]
    assert body["systemInstruction"]["parts"][0]["text"] == "You are a file agent."
    params = body["tools"][0]["functionDeclarations"][0]["parameters"]
    assert "additionalProperties" not in params  # not accepted by Gemini
    assert params["properties"]["limit"] == {"type": "integer", "nullable": True}
    assert body["contents"][-1]["parts"][0]["functionResponse"]["name"] == "search_files"
    assert result.tool_calls == [ToolCall(name="search_files", arguments={"query": "b"})]


def test_extract_json() -> None:
    assert extract_json('Sure! {"a": 1} hope that helps') == '{"a": 1}'
    assert extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'
