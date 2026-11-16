"""Step 7: the routing case file is well formed (the live score comes from
`python -m agents.router.eval_routing`, which needs the local model)."""

from __future__ import annotations

from collections import Counter

from agents.router.eval_routing import ROUTER_ACTIONS, load_cases

# Master document 4.3 catalogue ids (+ the Step 7 echo agent). Home/Device comes later.
CATALOGUE = {
    "echo",
    "file_system",
    "coding",
    "debugging",
    "image_editing",
    "web_research",
    "app_automation",
    "memory_librarian",
    "tool_builder",
    "communication",
    "planner",
    "documents_study",
}


def test_forty_well_formed_cases() -> None:
    cases = load_cases()
    assert len(cases) >= 40
    assert len({c.message for c in cases}) == len(cases), "duplicate message"
    for case in cases:
        assert case.expect in CATALOGUE or case.expect in ROUTER_ACTIONS, case


def test_unclear_requests_are_covered() -> None:
    counts = Counter(c.expect for c in load_cases())
    assert counts["clarify"] >= 5
    assert counts["answer"] >= 3
    assert counts["echo"] >= 3
