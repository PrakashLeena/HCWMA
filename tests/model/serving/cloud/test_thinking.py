"""Thinking ("reasoning") models (2026-11-26 diagnosis): the thinking-off switch, room for
thinking in max_tokens when it stays on, and an empty answer is an error with a reason."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from model.serving.cloud import providers
from model.serving.cloud.dispatcher import CloudUnavailable
from model.serving.cloud.providers import (
    REASONING_HEADROOM,
    CloudError,
    GeminiBackend,
    OpenAICompatBackend,
)
from model.serving.cloud.settings import CloudSettings, Profile
from model.serving.types import Message
from tests.model.serving.cloud.fake_server import fake_provider, openai_reply
from tests.model.serving.cloud.test_cloud_mode import _setup

KEY = "nvapi-test-key-not-real-0006"  # noqa: S105
HI = [Message(role="user", content="hi")]


@pytest.fixture(autouse=True)
def _fresh_switch_memory() -> Iterator[None]:
    providers._NO_THINKING_SWITCH.clear()
    yield
    providers._NO_THINKING_SWITCH.clear()


def test_thinking_off_sends_the_switch_and_the_plain_budget() -> None:
    with fake_provider() as fake:
        fake.reply(openai_reply(text="hello"))
        OpenAICompatBackend(base_url=fake.url, model="m", api_key=KEY, thinking_off=True).generate(
            HI, max_tokens=300
        )
    body = fake.last["json"]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["max_tokens"] == 300


def test_thinking_on_gets_headroom_and_no_switch() -> None:
    with fake_provider() as fake:
        fake.reply(openai_reply(text="hello"))
        OpenAICompatBackend(base_url=fake.url, model="m", api_key=KEY).generate(HI, max_tokens=300)
    body = fake.last["json"]
    assert "chat_template_kwargs" not in body
    assert body["max_tokens"] == 300 + REASONING_HEADROOM


def test_a_provider_that_refuses_the_switch_is_asked_again_without_it() -> None:
    with fake_provider() as fake:
        fake.reply({"error": "unknown field chat_template_kwargs"}, status=400)
        fake.reply(openai_reply(text="hello"))
        fake.reply(openai_reply(text="again"))
        backend = OpenAICompatBackend(base_url=fake.url, model="m", api_key=KEY, thinking_off=True)
        assert backend.generate(HI, max_tokens=100).text == "hello"
        assert backend.generate(HI, max_tokens=100).text == "again"
    first, retry, later = (r["json"] for r in fake.requests)
    assert "chat_template_kwargs" in first and first["max_tokens"] == 100
    assert "chat_template_kwargs" not in retry and retry["max_tokens"] == 100 + REASONING_HEADROOM
    assert "chat_template_kwargs" not in later  # remembered: no wasted call


def test_an_answer_used_up_by_thinking_is_an_error() -> None:
    reply = openai_reply(text="")
    reply["choices"][0]["finish_reason"] = "length"
    reply["choices"][0]["message"]["reasoning_content"] = "Let me think about hi..."
    with fake_provider() as fake:
        fake.reply(reply)
        with pytest.raises(CloudError) as err:
            OpenAICompatBackend(base_url=fake.url, model="m", api_key=KEY).generate(HI)
    assert err.value.kind == "empty_length"
    with fake_provider() as fake:
        fake.reply(openai_reply(text="   "))
        with pytest.raises(CloudError) as err:
            OpenAICompatBackend(base_url=fake.url, model="m", api_key=KEY).generate(HI)
    assert err.value.kind == "empty"


def test_reasoning_text_is_never_taken_as_the_answer() -> None:
    reply = openai_reply(text='{"action": "answer"}')
    reply["choices"][0]["message"]["reasoning_content"] = "secret chain of thought"
    with fake_provider() as fake:
        fake.reply(reply)
        result = OpenAICompatBackend(base_url=fake.url, model="m", api_key=KEY).generate(
            HI, response_format={"type": "object"}
        )
    assert result.text == '{"action": "answer"}'


def test_empty_answer_in_the_app_says_why(tmp_path: Path) -> None:
    reply = openai_reply(text="")
    reply["choices"][0]["finish_reason"] = "length"
    with fake_provider() as fake:
        fake.reply(reply)
        d, local, _ = _setup(tmp_path, fake)
        with pytest.raises(CloudUnavailable) as err:
            d.generate(HI, agent="router")
    assert err.value.kind == "empty_length" and "Thinking off" in str(err.value)
    assert local.calls == 0


def test_gemini_switch_is_a_zero_thinking_budget() -> None:
    reply = {"candidates": [{"content": {"parts": [{"text": "hi"}]}}], "usageMetadata": {}}
    with fake_provider() as fake:
        fake.reply(reply)
        GeminiBackend(base_url=fake.url, model="g", api_key=KEY, thinking_off=True).generate(
            HI, max_tokens=50
        )
    config = fake.last["json"]["generationConfig"]
    assert config["thinkingConfig"] == {"thinkingBudget": 0} and config["maxOutputTokens"] == 50


def test_defaults_thinking_off_and_a_90_second_timeout(tmp_path: Path) -> None:
    assert Profile(label="x", base_url="https://a.test").thinking_off is True
    s = CloudSettings()
    assert s.request_timeout_s == 90.0
    s = CloudSettings.model_validate(
        {
            "profile": "nvidia",
            "profiles": {
                "nvidia": {
                    "label": "N",
                    "base_url": "https://a.test",
                    "model": "m",
                    "thinking_off": False,
                }
            },
        }
    )
    assert s.thinking_off is False  # mirrored from the active profile
    with fake_provider() as fake:
        fake.reply(openai_reply(text="ok"))
        d, _, _ = _setup(tmp_path, fake)  # profile saved without the field: thinking off
        d.generate(HI, agent="router", max_tokens=40)
    assert fake.last["json"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert fake.last["json"]["max_tokens"] == 40
