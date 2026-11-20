"""Router fast path: send very clear requests straight to an agent, without a model call.

The rules live in the agent cards (`fast_route`: `patterns` and `keywords`), never here, so
this module has no agent-specific branches (architecture rule 4). It routes only when:

- exactly one enabled agent's pattern matches the owner's message, and
- no other enabled agent's keyword appears in it (a mixed request goes to the model).

Anything else returns None and the model Router decides as before. A fast route delegates
the owner's own words as the goal, with full confidence; the agent, its card grant, the
policy layer and the audit log work exactly as for a model-routed request.
"""

from __future__ import annotations

import re
from functools import lru_cache

from agents.registry.agent_registry import AgentRegistry
from agents.registry.card import AgentCard
from agents.router.classifier import RouteStep, RoutingDecision

MAX_GOAL_CHARS = 500  # RouteStep.goal limit
FAST_CONFIDENCE = 1.0


@lru_cache(maxsize=512)
def _pattern(text: str) -> re.Pattern[str]:
    return re.compile(text, re.IGNORECASE)


@lru_cache(maxsize=512)
def _word(text: str) -> re.Pattern[str]:
    """A keyword as a whole word or phrase, any case, any spacing between its words."""
    words = r"\s+".join(re.escape(w) for w in text.split())
    return re.compile(rf"(?<!\w){words}(?!\w)", re.IGNORECASE)


def matches_pattern(card: AgentCard, message: str) -> bool:
    rules = card.fast_route
    return rules is not None and any(_pattern(p).search(message) for p in rules.patterns)


def mentions_keyword(card: AgentCard, message: str) -> str | None:
    """The first of the card's keywords found in the message, or None."""
    rules = card.fast_route
    if rules is None:
        return None
    return next((k for k in rules.keywords if _word(k).search(message)), None)


def fast_route(registry: AgentRegistry, message: str) -> RoutingDecision | None:
    """A delegation for a very clear request, or None (the model Router decides)."""
    text = message.strip()
    if not text:
        return None
    workers = registry.workers()
    matched = [card for card in workers if matches_pattern(card, text)]
    if len(matched) != 1:
        return None  # nothing clear, or two agents claim it
    chosen = matched[0]
    if any(mentions_keyword(other, text) for other in workers if other.id != chosen.id):
        return None  # it also sounds like another agent's job
    return RoutingDecision(
        action="delegate",
        confidence=FAST_CONFIDENCE,
        steps=[RouteStep(agent=chosen.id, goal=text[:MAX_GOAL_CHARS])],
    )


# ---------- live facts: the Router never states them itself (owner decision 2026-11-20) ----

_QUESTION = re.compile(
    r"\?|^\W*(what|when|where|which|who|whose|how|is|are|was|were|do|does|did|has|have|can|"
    r"could|any|show|tell|list|read|check|give|find)\b",
    re.IGNORECASE,
)
CANNOT_CHECK = "I can't check that: none of my tools can look it up, and I won't guess. {hint}"


def asks_live_fact(card: AgentCard, message: str) -> bool:
    return any(_pattern(p).search(message) for p in card.live_facts)


def live_fact_route(registry: AgentRegistry, message: str) -> RoutingDecision | None:
    """Replacement for a model-Router *answer* that would state a live fact.

    None: the message asks no live fact, so the Router's answer may stand. Otherwise: a
    delegation to the one agent whose card lists that fact, or an honest "I can't check".
    """
    text = message.strip()
    if not text or not _QUESTION.search(text):
        return None
    owners = [c for c in registry.workers() if asks_live_fact(c, text)]
    if len(owners) == 1:
        return RoutingDecision(
            action="delegate",
            confidence=FAST_CONFIDENCE,
            steps=[RouteStep(agent=owners[0].id, goal=text[:MAX_GOAL_CHARS])],
        )
    if owners:
        names = ", ".join(c.name for c in owners)
        hint = f"It could be for {names}: please say which one to check."
    elif asks_live_fact(registry.router, text):
        hint = "HCWMA has no tool for this yet."
    else:
        return None
    return RoutingDecision(
        action="answer", confidence=FAST_CONFIDENCE, answer=CANNOT_CHECK.format(hint=hint)
    )
