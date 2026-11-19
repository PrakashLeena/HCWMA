"""Step 7: card schema, loader and registry (master document 4.4, rules 2, 4, 5)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from agents.registry.agent_registry import AgentRegistry
from agents.registry.card import AgentCard, load_card
from integrations.mcp.tool_schemas import PermissionTier
from integrations.registry.capability_catalog import default_catalog
from tests.agents.harness import REPO_AGENTS, copy_real_agents, write_card


def _data(**kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "x",
        "name": "X",
        "status": "enabled",
        "description": "d",
        "handles": ["things"],
        "tools": [],
        "permission_tier": "T0",
        "system_prompt": "prompts/x.md",
    }
    data.update(kw)
    return data


def test_shipped_cards_load() -> None:
    registry = AgentRegistry.from_dir(REPO_AGENTS / "cards")
    assert registry.router.id == "router"
    assert registry.router.tools == []
    assert [c.id for c in registry.workers()] == [
        "app_automation",
        "coding",
        "communication",
        "debugging",
        "documents_study",
        "echo",
        "file_system",
        "image_editing",
        "memory_librarian",
        "planner",
        "system_settings",
        "tool_builder",
        "web_research",
    ]
    assert registry.prompt_for(registry.router).startswith("# HCWMA Router")


def test_every_card_tool_exists_in_the_catalogue() -> None:
    catalog = default_catalog()
    for path in (REPO_AGENTS / "cards").glob("*.yaml"):
        for tool in load_card(path).tools:
            assert catalog.get(tool) is not None, f"{path.name}: unknown tool {tool}"


def test_router_card_can_never_hold_tools() -> None:
    with pytest.raises(ValidationError, match="router card must not list any tools"):
        AgentCard.model_validate(_data(kind="router", tools=["get_system_time"]))
    with pytest.raises(ValidationError, match="T0"):
        AgentCard.model_validate(_data(kind="router", permission_tier="T2"))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"handles": []}, "handles"),
        ({"tools": ["a", "a"]}, "duplicate"),
        ({"approval_required": ["b"]}, "approval_required"),
        ({"system_prompt": "../secrets.md"}, "relative path"),
        ({"system_prompt": "C:/x.md"}, "relative path"),
        ({"id": "Bad-Id"}, "id"),
        ({"status": "on"}, "status"),
        ({"permission_tier": "T9"}, "permission_tier"),
        ({"surprise": 1}, "surprise"),
        ({"limits": {"max_steps": 500}}, "max_steps"),
        (
            {"memory_scopes": ["profile"], "prompt_memory_scopes": ["episodic"]},
            "prompt_memory_scopes",
        ),
        ({"direct_answer_tools": ["search_files"]}, "direct_answer_tools not on the card"),
        (
            {
                "tools": ["delete_file"],
                "approval_required": ["delete_file"],
                "direct_answer_tools": ["delete_file"],
            },
            "must not need approval",
        ),
    ],
)
def test_bad_cards_are_rejected(change: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        AgentCard.model_validate(_data(**change))


def test_card_id_must_match_file_name(tmp_path: Path) -> None:
    write_card(tmp_path, "good")
    path = tmp_path / "cards" / "good.yaml"
    path.rename(tmp_path / "cards" / "other.yaml")
    with pytest.raises(ValueError, match="must match the file name"):
        load_card(tmp_path / "cards" / "other.yaml")


def test_registry_needs_exactly_one_router(tmp_path: Path) -> None:
    write_card(tmp_path, "only_worker")
    with pytest.raises(ValueError, match="exactly one router"):
        AgentRegistry.from_dir(tmp_path / "cards")
    agents = copy_real_agents(tmp_path / "two")
    write_card(agents, "router2", kind="router", handles=[])
    with pytest.raises(ValueError, match="exactly one router"):
        AgentRegistry.from_dir(agents / "cards")


def test_missing_prompt_fails_at_start_up(tmp_path: Path) -> None:
    agents = copy_real_agents(tmp_path)
    write_card(agents, "ghost")
    (agents / "prompts" / "ghost.md").unlink()
    with pytest.raises(FileNotFoundError):
        AgentRegistry.from_dir(agents / "cards")


def test_grant_comes_from_the_card() -> None:
    card = AgentCard.model_validate(_data(tools=["get_system_time"], permission_tier="T1"))
    grant = card.grant()
    assert grant.agent_id == "x"
    assert grant.tools == frozenset({"get_system_time"})
    assert grant.max_tier == PermissionTier.T1
