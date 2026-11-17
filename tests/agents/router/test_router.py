"""Step 7 "Done when": Router → echo → back with a result; an unclear request produces a
clarifying question. Real registry, runtime, policy layer and MCP server; scripted model."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import anyio

from agents.router import classifier as classifier_module
from agents.router import router as router_module
from agents.router.router import RouterReply
from model.serving import GenerateResult, Message
from tests.agents.harness import (
    ScriptedModel,
    Stack,
    copy_real_agents,
    route,
    say,
    stack,
    tool_calls,
    write_card,
)


def _ask(tmp_path: Path, model: ScriptedModel, *turns: str, **kw: Any) -> tuple[RouterReply, Stack]:
    conversation = [
        Message(role="user" if i % 2 == 0 else "assistant", content=t) for i, t in enumerate(turns)
    ]

    async def go() -> tuple[RouterReply, Stack]:
        async with stack(tmp_path, model, **kw) as s:
            return await s.router.handle(conversation), s

    return anyio.run(go)


def test_request_goes_router_to_echo_and_back(tmp_path: Path) -> None:
    model = ScriptedModel(
        router=route(steps=[{"agent": "echo", "goal": "Echo 'hello HCWMA'"}]),
        agent=say("Echo: hello HCWMA"),
    )
    reply, s = _ask(tmp_path, model, "echo hello HCWMA")

    assert reply.action == "delegate"
    assert reply.status == "done"
    assert reply.agents == ["echo"]
    assert reply.reply == "Echo: hello HCWMA"
    # one routing call (no tools, structured output), one echo agent call
    router_call, agent_call = model.calls
    assert router_call.is_router and router_call.kwargs["tools"] is None
    assert "Echo Test Agent" in agent_call.messages[0].content  # echo's own prompt
    assert "Echo 'hello HCWMA'" in agent_call.messages[1].content
    # delegation written to the audit log: start and outcome
    records = s.audit.read_all()
    assert [(r.event, r.agent, r.target_agent, r.outcome) for r in records] == [
        ("delegation", "router", "echo", "delegated"),
        ("delegation", "router", "echo", "done"),
    ]
    assert records[0].parent_task_id == reply.task_id


def test_echo_uses_its_tool_through_mcp(tmp_path: Path) -> None:
    def agent(messages: list[Message], _: dict[str, Any]) -> GenerateResult:
        if messages[-1].role == "tool":
            return GenerateResult(text="It is now on this PC.")
        return tool_calls("get_system_time")

    model = ScriptedModel(
        router=route(steps=[{"agent": "echo", "goal": "Tell the current time"}]), agent=agent
    )
    reply, s = _ask(tmp_path, model, "what time is it?")

    assert reply.status == "done"
    assert reply.tool_calls == 1
    tool_records = [r for r in s.audit.read_all() if r.event == "tool_call"]
    assert [(r.agent, r.tool, r.outcome) for r in tool_records] == [
        ("echo", "get_system_time", "ok")
    ]
    tool_turn = model.agent_calls()[-1].messages[-1]
    assert tool_turn.role == "tool" and '"local"' in tool_turn.content


def test_unclear_request_gets_a_clarifying_question(tmp_path: Path) -> None:
    model = ScriptedModel(
        router=route("clarify", 0.3, question="Which file do you mean, and what should I do?")
    )
    reply, s = _ask(tmp_path, model, "do the thing with it")

    assert reply.action == "clarify"
    assert reply.status == "needs_clarification"
    assert reply.reply == "Which file do you mean, and what should I do?"
    assert reply.agents == []
    assert s.audit.read_all() == []  # nothing was delegated
    assert len(model.calls) == 1


def test_low_confidence_delegation_becomes_a_question(tmp_path: Path) -> None:
    model = ScriptedModel(
        router=route(
            confidence=0.4,
            steps=[{"agent": "echo", "goal": "maybe echo?"}],
            question="Do you want me to repeat that back?",
        )
    )
    reply, _ = _ask(tmp_path, model, "hmm, that")
    assert reply.action == "clarify"
    assert reply.reply == "Do you want me to repeat that back?"
    assert model.agent_calls() == []


def test_unknown_or_router_agent_is_never_delegated_to(tmp_path: Path) -> None:
    for target in ("image_editing", "router", "shell"):  # image_editing: not built yet
        model = ScriptedModel(router=route(steps=[{"agent": target, "goal": "x"}]))
        reply, s = _ask(tmp_path / target, model, "find my file")
        assert reply.action == "clarify", target
        assert model.agent_calls() == []
        assert s.audit.read_all() == []


def test_invalid_model_output_is_a_question_not_a_guess(tmp_path: Path) -> None:
    for raw in (
        "not json",
        '{"action": "run_shell", "confidence": 1}',
        "[]",
        '{"action":"answer","confidence":1,"answer":"  "}',
    ):
        model = ScriptedModel(router=say(raw))
        reply, _ = _ask(tmp_path, model, "hello")
        assert reply.action == "clarify"
        assert reply.reply == classifier_module.FALLBACK_QUESTION


def test_router_can_answer_small_talk(tmp_path: Path) -> None:
    model = ScriptedModel(router=route("answer", 0.95, answer="Hi! How can I help?"))
    reply, s = _ask(tmp_path, model, "hi")
    assert (reply.action, reply.status, reply.reply) == ("answer", "done", "Hi! How can I help?")
    assert s.audit.read_all() == []


def test_history_is_used_for_routing(tmp_path: Path) -> None:
    model = ScriptedModel(router=route("answer", 0.9, answer="ok"))
    _ask(tmp_path, model, "do the thing", "Which thing?", "echo the word apple")
    sent = model.calls[0].messages
    assert [m.role for m in sent] == ["system", "user", "assistant", "user"]


def test_catalogue_comes_from_cards_with_no_router_code_change(tmp_path: Path) -> None:
    agents = copy_real_agents(tmp_path / "agents")
    write_card(agents, "weather", handles=["weather forecast"], description="Tells the weather.")
    write_card(agents, "sleepy", status="disabled", handles=["naps"])
    model = ScriptedModel(
        router=route(steps=[{"agent": "weather", "goal": "Forecast for today"}]),
        agent=say("Sunny."),
    )
    reply, _ = _ask(tmp_path, model, "weather?", agents_dir=agents)

    prompt = model.calls[0].messages[0].content
    assert "id: weather" in prompt and "weather forecast" in prompt
    assert "id: echo" in prompt
    assert "sleepy" not in prompt  # disabled agents are not offered
    assert "id: router" not in prompt
    assert (reply.agents, reply.reply) == (["weather"], "Sunny.")


def test_multi_step_stops_at_the_first_unfinished_step(tmp_path: Path) -> None:
    agents = copy_real_agents(tmp_path / "agents")
    write_card(agents, "second", handles=["second things"])
    model = ScriptedModel(
        router=route(
            steps=[{"agent": "echo", "goal": "first"}, {"agent": "second", "goal": "then this"}]
        ),
        agent=say("NEEDS_CLARIFICATION: echo what, exactly?"),
    )
    reply, _ = _ask(tmp_path, model, "echo, then second", agents_dir=agents)
    assert reply.agents == ["echo"]
    assert reply.status == "needs_clarification"
    assert reply.reply == "echo what, exactly?"


def test_failed_agent_is_reported_honestly(tmp_path: Path) -> None:
    model = ScriptedModel(router=route(steps=[{"agent": "echo", "goal": "echo"}]), agent=say("   "))
    reply, _ = _ask(tmp_path, model, "echo")
    assert reply.status == "failed"
    assert reply.reply.startswith("The echo agent could not finish")


def test_router_code_has_no_agent_specific_branches() -> None:
    for module in (router_module, classifier_module):
        source = inspect.getsource(module)
        for agent_id in ("echo", "file_system", "coding"):
            assert f'"{agent_id}"' not in source and f"'{agent_id}'" not in source
