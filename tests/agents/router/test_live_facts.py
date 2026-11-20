"""Owner decision 2026-11-20: the Router has no tools, so its "answer" action must never
state a live fact (time, date, files, emails, settings). Such a request goes to the agent
that can check it, or the Router says it cannot check. The rules come from the cards."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from agents.registry.agent_registry import AgentRegistry
from agents.router.fast_path import live_fact_route
from agents.router.router import RouterReply
from model.serving import GenerateResult, Message
from tests.agents.harness import ScriptedModel, Stack, route, stack, tool_calls

REGISTRY = AgentRegistry.from_dir()


@pytest.mark.parametrize(
    ("message", "agent"),
    [
        ("what's the time on my laptop now please?", "echo"),
        ("is my bluetooth on?", "system_settings"),
        ("what is in my latest email?", "communication"),
        ("what does notes.txt say?", "documents_study"),
        ("how much free space is on my disk?", "file_system"),
        ("any reminders for tomorrow?", "planner"),
        ("Do you already have a tool that counts words?", "tool_builder"),
    ],
)
def test_live_fact_questions_go_to_the_agent_that_can_check(message: str, agent: str) -> None:
    decision = live_fact_route(REGISTRY, message)
    assert decision is not None and decision.action == "delegate"
    assert [s.agent for s in decision.steps] == [agent]


def test_facts_no_agent_can_check_get_an_honest_cannot_check() -> None:
    decision = live_fact_route(REGISTRY, "what's the weather in Colombo today?")
    assert decision is not None and decision.action == "answer"
    assert decision.answer.startswith("I can't check that")


@pytest.mark.parametrize(
    "message",
    [
        "thanks!",
        "hello",
        "what can you do?",
        "thanks for finding the file",
        "which apps can you handle?",
    ],
)
def test_small_talk_and_questions_about_hcwma_may_be_answered(message: str) -> None:
    assert live_fact_route(REGISTRY, message) is None


def _ask(tmp_path: Path, model: ScriptedModel, text: str) -> tuple[RouterReply, Stack]:
    async def go() -> tuple[RouterReply, Stack]:
        async with stack(tmp_path, model) as s:
            return await s.router.handle([Message(role="user", content=text)]), s

    return anyio.run(go)


def test_a_router_answer_with_a_made_up_time_is_replaced_by_the_echo_agent(tmp_path: Path) -> None:
    calls: list[str] = []

    def echo(messages: list[Message], _k: object) -> GenerateResult:
        if not any(m.role == "tool" for m in messages):
            calls.append("tool")
            return tool_calls("get_system_time")
        return GenerateResult(text="It is the time the tool said.")

    model = ScriptedModel(router=route("answer", answer="It is 3:00 PM."), agent=echo)
    reply, s = _ask(tmp_path, model, "could you tell me the time on my laptop at the moment?")
    assert "3:00 PM" not in reply.reply and reply.agents == ["echo"] and calls == ["tool"]
    tools = [r.tool for r in s.audit.read_all() if r.event == "tool_call"]
    assert tools == ["get_system_time"]


def test_a_router_answer_about_the_weather_says_it_cannot_check(tmp_path: Path) -> None:
    model = ScriptedModel(router=route("answer", answer="It is sunny and 31 degrees."))
    reply, _ = _ask(tmp_path, model, "what's the weather like today?")
    assert "sunny" not in reply.reply and reply.reply.startswith("I can't check that")


def test_a_router_answer_to_small_talk_is_kept(tmp_path: Path) -> None:
    model = ScriptedModel(router=route("answer", answer="You're welcome!"))
    reply, _ = _ask(tmp_path, model, "thanks a lot")
    assert reply.reply == "You're welcome!"


def test_router_card_lists_facts_no_agent_can_check() -> None:
    assert REGISTRY.router.live_facts and REGISTRY.router.tools == []
