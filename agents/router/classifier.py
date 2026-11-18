"""Routing decision: which agent, or ask, or answer (master document 4.2).

The model returns JSON that matches `RoutingDecision` (Ollama structured output). The
agent list in the prompt is built from the registry's cards, so a new agent needs no code
change here (rule 4). Anything the model returns is re-checked in code:

- an agent that is not an enabled worker is never delegated to,
- confidence below the threshold becomes a clarifying question,
- invalid output becomes a clarifying question, never a guess.
"""

from __future__ import annotations

import json
import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agents.registry.agent_registry import AgentRegistry
from agents.registry.card import AgentCard
from agents.runtime.agent_runtime import AgentRuntime
from model.serving import Message

log = logging.getLogger(__name__)

DEFAULT_CONFIDENCE_THRESHOLD = 0.6
MAX_HISTORY_TURNS = 6
MAX_ROUTE_STEPS = 3
ROUTER_MAX_TOKENS = 300  # short prompts, low output limit: routing stays fast (5.5)
RAW_LOG_CHARS = 1000
FALLBACK_QUESTION = "I'm not sure what you'd like me to do. Could you say a bit more?"


class RouteStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: str = Field(max_length=40)
    goal: str = Field(min_length=1, max_length=500)


class RoutingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["delegate", "clarify", "answer"]
    confidence: float = Field(ge=0, le=1)
    steps: list[RouteStep] = Field(default_factory=list, max_length=MAX_ROUTE_STEPS)
    question: str = Field(default="", max_length=500)
    answer: str = Field(default="", max_length=2000)


def render_catalogue(workers: list[AgentCard]) -> str:
    if not workers:
        return "(no agents are enabled yet)"
    return "\n".join(f"- {card.id}: {card.routing_line()}" for card in workers)


class Classifier:
    def __init__(
        self,
        registry: AgentRegistry,
        runtime: AgentRuntime,
        threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    ) -> None:
        self._registry = registry
        self._runtime = runtime
        self.threshold = threshold

    def system_prompt(self, memory_block: str = "") -> str:
        base = self._registry.prompt_for(self._registry.router)
        prompt = f"{base}\n\n## Available agents\n\n{render_catalogue(self._registry.workers())}"
        if memory_block:
            # Quoted data from the memory service (Router card scopes only), never rules.
            prompt += f"\n\n## What HCWMA remembers\n\n{memory_block}"
        return prompt

    async def classify(
        self, conversation: list[Message], memory_block: str = "", task_id: str | None = None
    ) -> RoutingDecision:
        turns = [m for m in conversation if m.role in ("user", "assistant")][-MAX_HISTORY_TURNS:]
        messages = [Message(role="system", content=self.system_prompt(memory_block))]
        messages += [Message(role=m.role, content=m.content) for m in turns]
        result = await self._runtime.generate(
            messages,
            None,  # the Router has no tools (rule 2)
            max_tokens=ROUTER_MAX_TOKENS,
            response_format=RoutingDecision.model_json_schema(),
            temperature=0,
            seed=0,  # fixed seed: the same request always routes the same way
            audit_as=None if task_id is None else ("router", task_id),
        )
        return self.check(result.text)

    def check(self, raw: str) -> RoutingDecision:
        """Validate the model's JSON and apply the code-side routing rules."""
        try:
            decision = RoutingDecision.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            _log_failure(f"invalid routing JSON ({type(exc).__name__})", raw)
            return _clarify(FALLBACK_QUESTION)

        if decision.action == "answer":
            if not decision.answer.strip():
                _log_failure("action 'answer' with an empty `answer`", raw)
                return _clarify(FALLBACK_QUESTION)
            return decision
        if decision.action == "clarify":
            return _clarify(decision.question.strip() or FALLBACK_QUESTION, decision.confidence)

        # delegate
        enabled = {c.id for c in self._registry.workers()}
        if not decision.steps or any(s.agent not in enabled for s in decision.steps):
            return _clarify(decision.question.strip() or FALLBACK_QUESTION, decision.confidence)
        if decision.confidence < self.threshold:
            return _clarify(decision.question.strip() or FALLBACK_QUESTION, decision.confidence)
        return decision


def _log_failure(reason: str, raw: str) -> None:
    """Every routing parse failure is logged with the model's raw output (capped)."""
    log.warning("router output rejected: %s; raw=%r", reason, raw[:RAW_LOG_CHARS])


def _clarify(question: str, confidence: float = 0.0) -> RoutingDecision:
    return RoutingDecision(action="clarify", confidence=confidence, question=question)
