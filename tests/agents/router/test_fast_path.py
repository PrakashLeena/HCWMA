"""Router fast path: rules from the cards route very clear requests without a model call."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest
from pydantic import ValidationError

from agents.registry.agent_registry import AgentRegistry
from agents.registry.card import AgentCard
from agents.router.eval_routing import load_cases
from agents.router.fast_path import fast_route
from model.serving import GenerateResult, Message
from tests.agents.harness import REPO_AGENTS, ScriptedModel, copy_real_agents, say, stack

REGISTRY = AgentRegistry.from_dir(REPO_AGENTS / "cards")


def _agent(message: str) -> str | None:
    decision = fast_route(REGISTRY, message)
    return None if decision is None else decision.steps[0].agent


def test_every_fast_routed_case_goes_to_its_expected_agent() -> None:
    fast = [(c, _agent(c.message)) for c in load_cases()]
    fast = [(c, got) for c, got in fast if got is not None]
    assert len(fast) >= 8  # the fast path is used at all
    wrong = [(c.message, c.expect, got) for c, got in fast if got != c.expect]
    assert wrong == []


@pytest.mark.parametrize(
    ("message", "agent"),
    [
        ("Where is the file called invoice-march.pdf?", "file_system"),
        ("find amma folder", "file_system"),
        ("I lost a file called annek-passwords, please find it", "file_system"),
        ("Remember that my sister's birthday is on 12 March", "memory_librarian"),
        ("forget my old address", "memory_librarian"),
        ("What time is it?", "echo"),
        ("what is today's date", "echo"),
        # owner request 2026-11-20: trailing words, and picture dimensions
        ("What time is it on this PC?", "echo"),
        ("what's the time right now on my laptop please", "echo"),
        ("What size is the picture workspace/smoke/photo.png?", "image_editing"),
        ("what are the dimensions of holiday.jpg", "image_editing"),
        ("how many pixels is my screenshot", "image_editing"),
    ],
)
def test_clear_requests_take_the_fast_path(message: str, agent: str) -> None:
    decision = fast_route(REGISTRY, message)
    assert decision is not None and decision.action == "delegate"
    assert decision.confidence == 1.0
    assert [(s.agent, s.goal) for s in decision.steps] == [(agent, message)]


@pytest.mark.parametrize(
    "message",
    [
        "find the bug in my file",  # debugging keyword
        "write a script to find missing files",  # coding keywords
        "find my holiday photos folder",  # image keyword
        "find the file I downloaded from the internet",  # web keywords
        "click the button that finds my file",  # app keyword
        "where is that file?",  # needs the conversation
        "find it in this folder",  # needs the conversation
        "Forget that, I don't want you to keep it",  # needs the conversation
        "forget it",
        "remember that my project files live in Documents",  # also a file request
        "what time is it in Tokyo?",  # not the PC's clock
        "Remind me to find the file tomorrow",  # memory keyword vs file pattern
        "please find my thesis draft",  # no file/folder word: unclear enough for the model
        "",
        "   ",
    ],
)
def test_anything_less_clear_goes_to_the_model(message: str) -> None:
    assert fast_route(REGISTRY, message) is None


def test_a_disabled_agent_has_no_fast_path(tmp_path: Path) -> None:
    agents = copy_real_agents(tmp_path / "agents")
    card = agents / "cards" / "echo.yaml"
    card.write_text(
        card.read_text(encoding="utf-8").replace("status: enabled", "status: disabled"),
        encoding="utf-8",
    )
    registry = AgentRegistry.from_dir(agents / "cards")
    assert fast_route(registry, "What time is it?") is None


def _card(**kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "x",
        "name": "X",
        "status": "enabled",
        "description": "d",
        "handles": ["things"],
        "permission_tier": "T0",
        "system_prompt": "prompts/x.md",
    }
    data.update(kw)
    return data


def test_cards_reject_bad_patterns_and_router_fast_routes() -> None:
    with pytest.raises(ValidationError, match="does not compile"):
        AgentCard.model_validate(_card(fast_route={"patterns": ["(unclosed"]}))
    with pytest.raises(ValidationError, match="router card cannot have fast_route"):
        AgentCard.model_validate(_card(kind="router", handles=[], fast_route={"keywords": ["x"]}))


def test_fast_path_skips_the_router_model_and_is_audited(tmp_path: Path) -> None:
    def router_must_not_run(_m: list[Message], _k: dict[str, Any]) -> GenerateResult:
        raise AssertionError("the model Router was called")

    model = ScriptedModel(router=router_must_not_run, agent=say("It is 10:00 on this PC."))

    async def go() -> Any:
        async with stack(tmp_path, model) as s:
            return await s.router.handle([Message(role="user", content="What time is it?")]), s

    reply, s = anyio.run(go)
    assert reply.action == "delegate" and reply.agents == ["echo"] and reply.status == "done"
    assert [c for c in model.calls if c.is_router] == []
    records = s.audit.read_all()
    assert [(r.event, r.agent, r.target_agent) for r in records if r.event == "delegation"] == [
        ("delegation", "router", "echo"),
        ("delegation", "router", "echo"),
    ]
    assert not any(r.event == "model_call" and r.agent == "router" for r in records)
