"""Score the Router against `tests/agents/routing_cases.yaml` with the real local model:

    python -m agents.router.eval_routing            (~3 s per case on CPU)

Only routing decisions are made (no agent runs, no tools). Cases whose expected agent is
not enabled yet are listed as skipped, not scored.
"""

from __future__ import annotations

import time
from pathlib import Path

import anyio
import yaml
from pydantic import BaseModel, ConfigDict

from agents.registry.agent_registry import AgentRegistry
from agents.router.classifier import Classifier, RoutingDecision
from agents.runtime.agent_runtime import AgentRuntime
from agents.runtime.model_queue import get_model_queue
from model.serving import Message
from security.audit.audit_logger import get_audit_logger
from security.permissions.permission_manager import load_permissions_config

CASES_PATH = Path(__file__).resolve().parents[2] / "tests" / "agents" / "routing_cases.yaml"
ROUTER_ACTIONS = ("clarify", "answer")


class RoutingCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    expect: str


def load_cases(path: Path = CASES_PATH) -> list[RoutingCase]:
    with path.open(encoding="utf-8") as f:
        return [RoutingCase.model_validate(c) for c in yaml.safe_load(f)["cases"]]


def outcome(decision: RoutingDecision) -> str:
    if decision.action == "delegate":
        return decision.steps[0].agent
    return decision.action


class _NoGateway:
    async def list_tools_for(self, agent: object) -> list[object]:
        return []

    async def call_tool(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError("routing eval never calls tools")


async def run() -> int:
    registry = AgentRegistry.from_dir()
    runtime = AgentRuntime(
        registry=registry,
        queue=get_model_queue(),
        gateway=_NoGateway(),  # type: ignore[arg-type]
        audit=get_audit_logger(),
        limits=load_permissions_config().limits,
    )
    classifier = Classifier(registry, runtime)
    enabled = {c.id for c in registry.workers()}

    scored = passed = 0
    skipped: list[str] = []
    for case in load_cases():
        if case.expect not in enabled and case.expect not in ROUTER_ACTIONS:
            skipped.append(case.expect)
            continue
        start = time.perf_counter()
        decision = await classifier.classify([Message(role="user", content=case.message)])
        got = outcome(decision)
        ok = got == case.expect
        scored += 1
        passed += ok
        mark = "PASS" if ok else "FAIL"
        print(
            f"{mark}  {time.perf_counter() - start:4.1f}s  expect={case.expect:<9} "
            f"got={got:<9} conf={decision.confidence:.2f}  {case.message!r}"
        )
    print(
        f"\nscored {passed}/{scored}; skipped {len(skipped)} (agent not built yet: "
        f"{', '.join(sorted(set(skipped)))})"
    )
    return 0 if scored and passed == scored else 1


def main() -> None:
    raise SystemExit(anyio.run(run))


if __name__ == "__main__":
    main()
