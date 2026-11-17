"""Step 11 "Done when" check, run by hand (needs Ollama running):

    python -m agents.router.demo_coding

The real stack: real Router and agents on Qwen, `configs/permissions.yaml` as it is, the
coding tools on an in-process MCP server (they are "quarantined" in the shipped catalogue
until the owner approves their manifests, so the demo registers them as approved for this run
only). Everything happens in a new folder `workspace/code/step11_demo_<time>/`.

1. Coding: "write a small program ... with a test" -> files appear in the workspace and the
   test passes when this script runs it again itself.
2. Debugging: a project with a planted bug (`cart.py` adds the discount instead of taking it
   off) and an unchanged test file -> after the agent, the tests pass, `cart.py` changed and
   the test file did not.
3. Nothing outside `workspace/code/` changed (repo folders and the rest of the workspace
   are compared before and after).

At the end every tool call made is shown from the audit log.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime
from pathlib import Path

import anyio

from agents.router.factory import open_router
from agents.router.router import Router, RouterReply
from integrations.adapters.pc.code_tools import (
    CodeToolDeps,
    RunTestsInput,
    _run_tests,
    code_tool_specs,
)
from integrations.registry.capability_catalog import CapabilityCatalog
from model.serving import Message
from security.audit.audit_logger import get_audit_logger
from security.permissions.config import REPO_ROOT
from security.permissions.path_check import PathPolicy

# a fresh folder per run: the demo never deletes anything (architecture rule 9)
DEMO = f"step11_demo_{datetime.now():%Y%m%d_%H%M%S}"
BUGGY = "def total(prices, discount=0):\n    return sum(prices) + discount\n"
TESTS = (
    "from cart import total\n\n\n"
    "def test_plain_total():\n    assert total([10, 20]) == 30\n\n\n"
    "def test_discount_is_taken_off():\n    assert total([50, 50], discount=10) == 90\n"
)
WATCHED = ("agents", "integrations", "memory", "security", "configs", "docs", "tests", "workspace")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(code_root: Path) -> dict[str, tuple[int, int]]:
    """(size, mtime) of every file the agents must not touch, outside `workspace/code/`."""
    seen: dict[str, tuple[int, int]] = {}
    skip = {"__pycache__", ".pytest_cache", ".ruff_cache", ".venv", ".git"}
    for name in WATCHED:
        for path in (REPO_ROOT / name).rglob("*"):
            if path.is_file() and not skip & set(path.parts) and code_root not in path.parents:
                if path.name == "audit.jsonl" or path.suffix in {".sqlite", ".log"}:
                    continue  # written by HCWMA itself on every call
                stat = path.stat()
                seen[str(path)] = (stat.st_size, int(stat.st_mtime))
    return seen


def prepare(deps: CodeToolDeps) -> Path:
    demo = deps.code_root / DEMO
    shop = demo / "shop"
    shop.mkdir(parents=True)
    (shop / "cart.py").write_text(BUGGY, encoding="utf-8")
    (shop / "test_cart.py").write_text(TESTS, encoding="utf-8")
    return demo


async def ask(router: Router, text: str) -> RouterReply:
    print(f"\n> {text}")
    reply = await router.handle([Message(role="user", content=text)])
    path = " -> ".join(["router", *reply.agents])
    stopped = f" | stopped by {reply.stopped_by}" if reply.stopped_by else ""
    print(f"  [{path} | {reply.action} | {reply.status} | tool calls {reply.tool_calls}{stopped}]")
    for line in reply.reply.strip().splitlines():
        print(f"  {line}")
    return reply


async def run() -> bool:
    audit = get_audit_logger()
    before_audit = len(audit.read_all())
    deps = CodeToolDeps(PathPolicy.from_config())
    demo = prepare(deps)
    shop_tests_before = _digest(demo / "shop" / "test_cart.py")
    shop_code_before = _digest(demo / "shop" / "cart.py")
    outside_before = snapshot(deps.code_root)

    catalog = CapabilityCatalog()
    for spec in code_tool_specs(deps, status="approved"):
        catalog.register(spec)

    print(f"coding workspace: {deps.code_root}")
    async with open_router(catalog=catalog) as router:
        wrote = await ask(
            router,
            f"Write a small Python program in {DEMO}/wordstats that counts how many times "
            "each word appears in a text, with a pytest test for it, and run the test.",
        )
        fixed = await ask(
            router,
            f"The tests in {DEMO}/shop fail. Find the bug in cart.py and fix it.",
        )

    wordstats_files = sorted(p.name for p in (demo / "wordstats").glob("*.py"))
    mine = _run_tests(deps, RunTestsInput(path=f"{DEMO}/wordstats"))
    shop = _run_tests(deps, RunTestsInput(path=f"{DEMO}/shop"))
    outside_after = snapshot(deps.code_root)

    new = audit.read_all()[before_audit:]
    calls = [r for r in new if r.event == "tool_call"]
    print(f"\n--- audit log: {audit.path} ---")
    for rec in calls:
        print(f"  {rec.ts}  {rec.agent} -> {rec.tool} {str(rec.args)[:90]}  [{rec.outcome}]")
    print(f"\nwordstats files: {wordstats_files}; test run now: {mine.summary}")
    print(f"shop test run now: {shop.summary}")

    made = wrote.tool_calls + fixed.tool_calls
    checks = {
        "router -> coding for the program request": wrote.agents == ["coding"],
        "the program and a test file are in the workspace": len(wordstats_files) >= 2,
        "the program's tests pass when run again here": bool(mine.passed)
        and mine.failed == 0
        and mine.errors == 0,
        "router -> debugging for the planted bug": fixed.agents == ["debugging"],
        "the planted bug is fixed: tests pass": shop.passed == 2 and shop.failed == 0,
        "the fix is in cart.py, not in the tests": _digest(demo / "shop" / "cart.py")
        != shop_code_before
        and _digest(demo / "shop" / "test_cart.py") == shop_tests_before,
        "nothing outside workspace/code/ was written": outside_after == outside_before,
        "every tool call is in the audit log": made >= 4 and len(calls) == made,
        "no tool call was blocked as a path escape": not any(
            "outside the coding workspace" in (r.reason or "") for r in calls
        ),
    }
    print()
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    passed = all(checks.values())
    print("\nSTEP 11 DONE-WHEN:", "PASS" if passed else "FAIL")
    return passed


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    raise SystemExit(0 if anyio.run(run) else 1)


if __name__ == "__main__":
    main()
