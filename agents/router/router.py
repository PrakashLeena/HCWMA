"""The Router mini agent (master document 4.2, architecture rules 2, 4, 14).

It does not do the work; it decides who does. It has no tools of its own: it can only
delegate (through the agent runtime), ask a clarifying question, or answer.

Not yet here: pulling memory (Step 10), asking the Tool Builder when nothing fits (Step 13).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agents.router.classifier import Classifier, RoutingDecision
from agents.runtime.agent_runtime import OWNER_DENIED, AgentRuntime
from agents.runtime.stop import STOPPED_BY_OWNER, StopControl
from agents.runtime.task_envelope import (
    TaskContext,
    TaskEnvelope,
    TaskResult,
    TaskStatus,
    new_task_id,
)
from model.serving import Message
from security.approvals.approval_service import ApprovalService

ROUTER_DEPTH = 0


class RouterReply(BaseModel):
    task_id: str
    action: Literal["delegate", "clarify", "answer"]
    reply: str
    status: TaskStatus
    agents: list[str] = Field(default_factory=list)
    tool_calls: int = 0
    stopped_by: str | None = None


class Router:
    def __init__(
        self,
        runtime: AgentRuntime,
        classifier: Classifier | None = None,
        *,
        stop: StopControl | None = None,
        approvals: ApprovalService | None = None,
    ) -> None:
        self._runtime = runtime
        self._classifier = classifier or Classifier(runtime.registry, runtime)
        self.id = runtime.registry.router.id
        # Held for the desktop app (backend endpoints). The Router never answers a prompt
        # and has no tool to do so: only the owner can, through the backend.
        self.stop = stop
        self.approvals = approvals

    async def handle(self, conversation: list[Message]) -> RouterReply:
        if not conversation or conversation[-1].role != "user":
            raise ValueError("the conversation must end with a user message")
        task_id = new_task_id()
        if self.stop is None:
            return await self._handle(task_id, conversation)
        reply: RouterReply | None = None
        with self.stop.track(task_id) as scope:
            reply = await self._handle(task_id, conversation)
        if scope.cancelled_caught or reply is None:
            return RouterReply(
                task_id=task_id,
                action="answer",
                reply="Stopped. Everything HCWMA was doing has been halted.",
                status="failed",
                stopped_by=STOPPED_BY_OWNER,
            )
        return reply

    async def _handle(self, task_id: str, conversation: list[Message]) -> RouterReply:
        decision = await self._classifier.classify(conversation)

        if decision.action == "clarify":
            return RouterReply(
                task_id=task_id,
                action="clarify",
                reply=decision.question,
                status="needs_clarification",
            )
        if decision.action == "answer":
            return RouterReply(
                task_id=task_id, action="answer", reply=decision.answer, status="done"
            )
        return await self._delegate(task_id, decision, conversation[-1].content)

    async def _delegate(
        self, task_id: str, decision: RoutingDecision, user_message: str
    ) -> RouterReply:
        budget = self._runtime.new_budget()
        done: list[TaskEnvelope] = []
        for step in decision.steps:
            envelope = await self._runtime.delegate(
                from_agent=self.id,
                to_agent=step.agent,
                goal=step.goal,
                budget=budget,
                root_task_id=task_id,
                parent_task_id=task_id,
                depth=ROUTER_DEPTH + 1,
                context=TaskContext(user_message=user_message),
                # The agent gets its own card grant. Anything above T2 still stops at the
                # approval prompt in the policy layer (build step 9).
                read_only=False,
            )
            done.append(envelope)
            if _result(envelope).status != "done":
                break  # later steps depend on this one; do not carry on blindly

        last = _result(done[-1])
        return RouterReply(
            task_id=task_id,
            action="delegate",
            reply=_compose(done),
            status=last.status,
            agents=[e.to_agent for e in done],
            tool_calls=budget.tool_calls,
            stopped_by=last.stopped_by,
        )


def _result(envelope: TaskEnvelope) -> TaskResult:
    if envelope.result is None:  # the runtime always sets one; never treat silence as success
        return TaskResult(status="failed", summary="the agent returned no result")
    return envelope.result


def _compose(envelopes: list[TaskEnvelope]) -> str:
    """Report each agent's result honestly; a failure is never presented as success."""
    parts = []
    for env in envelopes:
        result = _result(env)
        if result.status == "done":
            parts.append(result.summary)
        elif result.status == "needs_clarification":
            parts.append(result.summary)
        elif result.status == "needs_approval":
            parts.append(f"Nothing was changed, because it was not approved: {result.summary}")
        elif result.stopped_by == OWNER_DENIED:
            parts.append(result.summary)
        else:
            parts.append(f"The {env.to_agent} agent could not finish: {result.summary}")
    return "\n\n".join(parts)
