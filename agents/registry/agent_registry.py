"""Agent registry: every capability card in `agents/cards/` (master document 4, 4.4).

Cards are loaded from the folder; nothing is registered by hand. Adding an agent means
adding a card and a prompt, never changing Router code (rule 4).
"""

from __future__ import annotations

from pathlib import Path

from agents.registry.card import AGENTS_DIR, DEFAULT_CARDS_DIR, AgentCard, load_card


class AgentRegistry:
    def __init__(self, cards: list[AgentCard], agents_dir: Path = AGENTS_DIR) -> None:
        self._cards: dict[str, AgentCard] = {}
        for card in cards:
            if card.id in self._cards:
                raise ValueError(f"duplicate agent id '{card.id}'")
            self._cards[card.id] = card
        routers = [c for c in cards if c.kind == "router"]
        if len(routers) != 1:
            raise ValueError(f"exactly one router card is required, found {len(routers)}")
        self.agents_dir = agents_dir

    @classmethod
    def from_dir(cls, cards_dir: Path = DEFAULT_CARDS_DIR) -> AgentRegistry:
        cards = [load_card(p) for p in sorted(cards_dir.glob("*.yaml"))]
        registry = cls(cards, agents_dir=cards_dir.parent)
        for card in cards:  # fail at start-up, not mid-task, if a prompt is missing
            card.load_prompt(registry.agents_dir)
        return registry

    def get(self, agent_id: str) -> AgentCard | None:
        return self._cards.get(agent_id)

    @property
    def router(self) -> AgentCard:
        return next(c for c in self._cards.values() if c.kind == "router")

    def workers(self) -> list[AgentCard]:
        """Enabled agents the Router may delegate to."""
        return sorted(
            (c for c in self._cards.values() if c.kind == "worker" and c.enabled),
            key=lambda c: c.id,
        )

    def prompt_for(self, card: AgentCard) -> str:
        return card.load_prompt(self.agents_dir)
