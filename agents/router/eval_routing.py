"""Score the Router against `tests/agents/routing_cases.yaml` with the real local model:

    python -m agents.router.eval_routing            (~15 s per case on CPU)

Only routing decisions are made (no agent runs, no tools). Cases whose expected agent is
not enabled yet are listed as skipped, not scored.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import anyio
import yaml
from pydantic import BaseModel, ConfigDict

from agents.registry.agent_registry import AgentRegistry
from agents.router.classifier import Classifier, RoutingDecision
from agents.router.fast_path import fast_route
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
    # An agent this request must never go to. Lets a near-miss case be scored even while
    # its expected agent is not built yet (add-agent skill: "must NOT route to it").
    must_not: str | None = None


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

    def direct_answer(self, *args: object) -> None:
        return None


async def run(
    expect: set[str] | None = None, match: list[str] | None = None, fast: bool = True
) -> int:
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

    scored = passed = fast_count = fast_passed = 0
    skipped: list[str] = []
    for case in load_cases():
        if expect is not None and case.expect not in expect and case.message not in (match or []):
            continue
        expect_known = case.expect in enabled or case.expect in ROUTER_ACTIONS
        if not expect_known and case.must_not not in enabled:
            skipped.append(case.expect)
            continue
        start = time.perf_counter()
        decision = fast_route(registry, case.message) if fast else None
        route = "fast " if decision is not None else "model"
        if decision is None:
            decision = await classifier.classify([Message(role="user", content=case.message)])
        got = outcome(decision)
        ok = got == case.expect if expect_known else got != case.must_not
        scored += 1
        passed += ok
        if route == "fast ":
            fast_count += 1
            fast_passed += ok
        mark = "PASS" if ok else "FAIL"
        print(
            f"{mark}  {route} {time.perf_counter() - start:4.1f}s  "
            f"expect={case.expect if expect_known else 'not ' + str(case.must_not):<15} "
            f"got={got:<9} conf={decision.confidence:.2f}  {case.message!r}"
        )
    if fast:
        print(f"\nfast path: {fast_count} case(s), {fast_passed} of them correct")
    print(
        f"\nscored {passed}/{scored}; skipped {len(skipped)} (agent not built yet: "
        f"{', '.join(sorted(set(skipped)))})"
    )
    return 0 if scored and passed == scored else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Score the Router on routing_cases.yaml")
    parser.add_argument("--expect", nargs="+", help="only cases whose expected agent is listed")
    parser.add_argument("--message", action="append", help="also run the case with this message")
    parser.add_argument("--no-fast", action="store_true", help="model Router only, no fast path")
    args = parser.parse_args()
    expect = set(args.expect) if args.expect else None
    raise SystemExit(anyio.run(run, expect, args.message, not args.no_fast))


if __name__ == "__main__":
    main()
