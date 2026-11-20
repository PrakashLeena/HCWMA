"""Owner decision 2026-11-20: an agent that must look things up may not answer without a
successful tool call. Checked on every card and on the reason the owner sees."""

from __future__ import annotations

import pytest

from agents.registry.agent_registry import AgentRegistry
from agents.runtime.agent_runtime import ungrounded_reason
from agents.runtime.task_envelope import ToolCallRecord

MUST_LOOK_UP = {
    "web_research", "file_system", "communication", "documents_study", "image_editing",
    "planner", "system_settings", "app_automation", "tool_builder",
}  # fmt: skip
MAY_ANSWER = {"coding", "debugging", "memory_librarian", "echo"}


@pytest.fixture(scope="module")
def cards() -> dict[str, object]:
    registry = AgentRegistry.from_dir()
    return {c.id: c for c in [registry.router, *registry.workers()]}


def test_agents_that_look_things_up_require_a_tool(cards: dict[str, object]) -> None:
    for agent in MUST_LOOK_UP:
        assert cards[agent].requires_tool, agent  # type: ignore[attr-defined]
    for agent in MAY_ANSWER:
        assert not cards[agent].requires_tool, agent  # type: ignore[attr-defined]
    assert not cards["router"].requires_tool  # type: ignore[attr-defined]


def test_reason_names_tools_that_are_not_approved(cards: dict[str, object]) -> None:
    card = cards["web_research"]
    reason = ungrounded_reason(card, offered=set(), hidden=set(), records=[])  # type: ignore[arg-type]
    assert reason == "this tool is not approved yet: open_website, extract_page_information"


def test_reason_for_a_tool_that_failed(cards: dict[str, object]) -> None:
    card = cards["file_system"]
    offered = set(card.tools)  # type: ignore[attr-defined]
    rec = [ToolCallRecord(tool="search_files", ok=False, error_code="tool_error")]
    assert (
        ungrounded_reason(card, offered, set(), rec) == "'search_files' did not work (tool_error)"
    )  # type: ignore[arg-type]


def test_reason_when_no_tool_was_used(cards: dict[str, object]) -> None:
    card = cards["communication"]
    offered = set(card.tools)  # type: ignore[attr-defined]
    reason = ungrounded_reason(card, offered, set(), [])  # type: ignore[arg-type]
    assert "answered without using its tools" in reason


def test_tools_held_back_on_purpose_are_not_called_unapproved(cards: dict[str, object]) -> None:
    """Approval-gated tools left out of a lookup request are not 'not approved'."""
    card = cards["file_system"]
    hidden = set(card.approval_required)  # type: ignore[attr-defined]
    offered = set(card.tools) - hidden  # type: ignore[attr-defined]
    assert "not approved" not in ungrounded_reason(card, offered, hidden, [])  # type: ignore[arg-type]
