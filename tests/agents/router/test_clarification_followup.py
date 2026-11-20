"""Real-use bug (2026-11-19): after the Router asked "Do you want me to send this email?", the
owner's "yes" was routed as a brand-new request (to app_automation). A short answer to a
clarification must continue the ORIGINAL request, with the agent the question was about."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio

from agents.router.router import RouterReply
from integrations.registry.capability_catalog import default_catalog
from model.serving import GenerateResult, Message
from tests.agents.harness import ScriptedModel, Stack, route, stack

ORIGINAL = "sort out the sam@example.com situation"  # nothing here fast-routes
QUESTION = "Do you want me to go ahead with that?"


def _run(tmp: Path, model: ScriptedModel, turns: list[str]) -> tuple[list[RouterReply], Stack]:
    """The page's behaviour: keep the history, add the user's text, then HCWMA's reply."""

    async def go() -> tuple[list[RouterReply], Stack]:
        async with stack(tmp / "run", model, catalog=default_catalog()) as s:
            history: list[Message] = []
            replies: list[RouterReply] = []
            for text in turns:
                history.append(Message(role="user", content=text))
                reply = await s.router.handle(list(history))
                history.append(Message(role="assistant", content=reply.reply))
                replies.append(reply)
            return replies, s

    return anyio.run(go)


def _router_script(*decisions: dict[str, Any]) -> Any:
    queue = list(decisions)
    return lambda _m, _k: GenerateResult(text=json.dumps(queue.pop(0)))


def _router_messages(s: Stack) -> list[list[Message]]:
    return [c.messages for c in s.model.calls if c.is_router]


def test_yes_after_a_router_question_routes_the_original_request(tmp_path: Path) -> None:
    router = _router_script(
        {"action": "clarify", "confidence": 0.9, "question": QUESTION},
        {"action": "delegate", "confidence": 0.9, "steps": [{"agent": "echo", "goal": "do it"}]},
    )
    model = ScriptedModel(router=router, agent=lambda _m, _k: GenerateResult(text="Done."))
    replies, s = _run(tmp_path, model, [ORIGINAL, "yes"])

    assert replies[0].action == "clarify" and replies[0].reply == QUESTION
    assert replies[1].action == "delegate" and replies[1].agents == ["echo"]
    # the second routing call saw the original request with the answer in it, not a lone "yes"
    last = _router_messages(s)[1][-1].content
    assert ORIGINAL in last and QUESTION in last and "yes" in last
    assert len([m for m in _router_messages(s)[1] if m.role == "user"]) == 1


def test_an_answer_to_an_agents_question_goes_back_to_that_agent_without_routing(
    tmp_path: Path,
) -> None:
    asked_by_agent = ORIGINAL
    seen_goals: list[str] = []

    def agent(messages: list[Message], _: dict[str, Any]) -> GenerateResult:
        text = "\n".join(m.content for m in messages)
        seen_goals.append(text)
        if "The owner answered" not in text:
            return GenerateResult(text="NEEDS_CLARIFICATION: Which Gmail search do you want?")
        return GenerateResult(text="Searched.")

    router = _router_script(
        {
            "action": "delegate",
            "confidence": 0.9,
            "steps": [{"agent": "communication", "goal": "tidy the mail"}],
        }
    )
    model = ScriptedModel(router=router, agent=agent)
    replies, s = _run(tmp_path, model, [asked_by_agent, "unread ones from Sam"])

    assert replies[0].status == "needs_clarification" and replies[0].agents == ["communication"]
    assert (
        replies[1].agents == ["communication"] and replies[1].status == "failed"
    )  # no tool call: refused
    assert len(_router_messages(s)) == 1  # the second turn did not ask the model Router at all
    assert "unread ones from Sam" in seen_goals[-1] and "Which Gmail search" in seen_goals[-1]


def test_the_owners_answer_counts_as_the_owners_own_words(tmp_path: Path) -> None:
    """A recipient typed in the answer can be used by the agent that asked for it."""
    from tests.agents.test_communication import FakeGmail, _call, _catalog

    gmail = FakeGmail()
    script = [
        GenerateResult(text="NEEDS_CLARIFICATION: Who should get it?"),
        _call("draft_message", to=["bob@example.com"], subject="Hi", body="Hello"),
        GenerateResult(text="Draft saved."),
    ]
    router = route(steps=[{"agent": "communication", "goal": "draft a hello"}])
    model = ScriptedModel(router=router, agent=lambda _m, _k: script.pop(0))

    async def go() -> list[RouterReply]:
        async with stack(tmp_path / "run", model, catalog=_catalog(gmail)) as s:
            first = await s.router.handle([Message(role="user", content="draft a hello email")])
            history = [
                Message(role="user", content="draft a hello email"),
                Message(role="assistant", content=first.reply),
                Message(role="user", content="bob@example.com"),
            ]
            return [first, await s.router.handle(history)]

    replies = anyio.run(go)
    assert replies[1].status == "done"
    assert gmail.drafts == [(["bob@example.com"], "Hi", "Hello")]


def test_a_clear_new_request_is_not_taken_for_an_answer(tmp_path: Path) -> None:
    router = _router_script(
        {"action": "clarify", "confidence": 0.9, "question": QUESTION},
    )
    model = ScriptedModel(router=router, agent=lambda _m, _k: GenerateResult(text="It is noon."))
    replies, s = _run(tmp_path, model, [ORIGINAL, "what time is it"])
    assert replies[1].agents == ["echo"]  # fast path: its own request
    assert len(_router_messages(s)) == 1


def test_a_long_message_after_a_question_is_a_new_request(tmp_path: Path) -> None:
    long_text = "please " + "really " * 15 + "think about something else entirely"
    router = _router_script(
        {"action": "clarify", "confidence": 0.9, "question": QUESTION},
        {"action": "answer", "confidence": 0.9, "answer": "ok"},
    )
    replies, s = _run(tmp_path, ScriptedModel(router=router), [ORIGINAL, long_text])
    assert replies[1].action == "answer"
    assert _router_messages(s)[1][-1].content == long_text  # routed as typed


def test_a_yes_with_no_question_pending_is_routed_normally(tmp_path: Path) -> None:
    router = _router_script({"action": "clarify", "confidence": 0.9, "question": "About what?"})
    replies, s = _run(tmp_path, ScriptedModel(router=router), ["yes"])
    assert replies[0].action == "clarify"
    assert _router_messages(s)[0][-1].content == "yes"


def test_a_question_is_answered_only_once(tmp_path: Path) -> None:
    router = _router_script(
        {"action": "clarify", "confidence": 0.9, "question": QUESTION},
        {"action": "answer", "confidence": 0.9, "answer": "ok"},
        {"action": "clarify", "confidence": 0.9, "question": "About what?"},
    )
    replies, s = _run(tmp_path, ScriptedModel(router=router), [ORIGINAL, "yes", "yes"])
    assert [r.action for r in replies] == ["clarify", "answer", "clarify"]
    # the third turn's last message is a plain "yes" again: no stale question is replayed
    assert _router_messages(s)[2][-1].content == "yes"
