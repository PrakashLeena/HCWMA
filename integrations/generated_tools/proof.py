"""Step 13 "Done when", for real: one simple tool end to end with the real Windows Sandbox.

    python -m integrations.generated_tools.proof [--keep]

generate -> sandbox test (real VM) -> quarantined and uncallable (over the real stdio tool
server) -> owner approves -> callable -> one line edited -> quarantined again.

The tool is written by the proof itself with the same `scaffold_tool` the Tool Builder uses
(no model is involved, so the result does not depend on Qwen). It lives in the real
`integrations/generated/word_count/`; approval state and audit records go to a fresh folder
in `workspace/`. The tool folder is moved to the Recycle Bin at the end unless `--keep`.
Needs Windows Sandbox and `workspace/sandbox/python` (see `sandbox_proof.py`); ~2 minutes.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

import anyio
from mcp import Client

from agents.runtime.policy import PolicyLayer
from integrations.adapters.local_data.tool_builder_tools import (
    NameInput,
    ScaffoldInput,
    ToolBuilderDeps,
    _run_tool_tests,
    _scaffold,
    _submit,
)
from integrations.generated_tools.manifest import REPO_ROOT, generated_root
from integrations.generated_tools.registry import granted_tool_names
from integrations.generated_tools.review import ReviewService
from integrations.generated_tools.store import GeneratedStore, _to_recycle_bin
from integrations.mcp.client import McpToolClient, local_server_params
from integrations.mcp.tool_schemas import PermissionTier
from integrations.registry.capability_catalog import default_catalog
from security.audit.audit_logger import AuditLogger
from security.permissions.permission_manager import (
    AgentGrant,
    PermissionManager,
    load_permissions_config,
)

MANIFEST = """\
name: word_count
version: 0.1.0
created_by: tool_builder
status: quarantined
route: local_data
description: Count the words, lines and characters in a text.
inputs:
  text: string
outputs:
  words: integer
  lines: integer
  characters: integer
permissions:
  network: []
  filesystem: []
  secrets: []
side_effects: none
approval_required: false
"""

ADAPTER = """\
import re


def run(args, ctx):
    text = args["text"]
    words = len(re.findall(r"\\S+", text))
    lines = len(text.splitlines())
    return {"words": words, "lines": lines, "characters": len(text)}
"""

TESTS = """\
import unittest

from adapter import run


class WordCountTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(run({"text": ""}, None), {"words": 0, "lines": 0, "characters": 0})

    def test_normal(self):
        out = run({"text": "one two  three\\nfour"}, None)
        self.assertEqual(out, {"words": 4, "lines": 2, "characters": 19})


if __name__ == "__main__":
    unittest.main()
"""

CHECKS: list[tuple[str, bool]] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((label, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> int:
    keep = "--keep" in sys.argv
    stamp = time.strftime("%Y%m%d_%H%M%S")
    work = REPO_ROOT / "workspace" / f"step13_proof_{stamp}"
    work.mkdir(parents=True)
    os.environ["HCWMA_DATA_DIR"] = str(work / "data")  # approval state: fresh, not the real one
    audit = AuditLogger(work / "audit.jsonl")
    call_audit = AuditLogger(work / "calls.jsonl")
    deps = ToolBuilderDeps(audit=lambda: audit)  # real PathPolicy, real Windows Sandbox
    folder = generated_root() / "word_count"
    if folder.exists():
        print(f"{folder} already exists; move it away first.")
        return 2
    owner = ReviewService(GeneratedStore(), audit, ["echo", "file_system"])

    def status() -> str | None:
        spec = default_catalog().get("word_count")
        return spec.manifest.status if spec else None

    async def call(agent_id: str, text: str) -> Any:
        grant = AgentGrant(
            agent_id=agent_id,
            tools=frozenset({"word_count"}) if agent_id == "echo" else granted_tool_names(agent_id),
            max_tier=PermissionTier.T2,
        )
        async with McpToolClient(Client(local_server_params())) as client:
            policy = PolicyLayer(
                client=client,
                catalog=default_catalog(),
                permissions=PermissionManager(load_permissions_config()),
                audit=call_audit,
            )
            direct = await client.call_tool("word_count", {"text": text})
            return await policy.call_tool("proof", grant, "word_count", {"text": text}), direct

    try:
        print("1. GENERATE — scaffold_tool writes the three files and the tool starts quarantined")
        out = _scaffold(
            deps,
            ScaffoldInput(
                name="word_count", manifest_yaml=MANIFEST, adapter_code=ADAPTER, test_code=TESTS
            ),
        )
        check("files written, no static-check problems", out.written and not out.problems)
        check("status is quarantined", status() == "quarantined")

        print("2. TEST — run_tool_tests, inside a real Windows Sandbox (not on this PC)")
        result = _run_tool_tests(deps, NameInput(name="word_count"))
        check("sandbox ran", result.ran, f"{result.seconds}s")
        check("tests passed", result.passed, f"{result.tests_run} tests")
        check("sandbox destroyed", result.sandbox_destroyed is True)
        last = audit.read_all()[-1]
        check(
            "sandbox run audited",
            last.event == "generated_tool" and last.args["action"] == "sandbox_test",
        )

        print("3. QUARANTINED — nobody can call it yet (real stdio tool server)")
        via, direct = anyio.run(call, "echo", "one two")
        check("gateway refuses it", via.error_code == "denied" and "quarantined" in via.error)
        check("tool server refuses it too", direct.error_code == "unknown_tool")

        print("4. REVIEW + APPROVE — the owner sees summary, permissions and full code")
        sub = _submit(deps, NameInput(name="word_count"))
        check("submitted", sub.submitted)
        review = owner.review("word_count")
        check("review shows summary", "Count the words" in review["summary"])
        check("review shows permissions", review["permissions"]["network"] == [])
        check("review shows full code", review["adapter_code"] == ADAPTER)
        approved = owner.approve("word_count", review["hash"], ["echo"])
        check("approved by the owner", approved["status"] == "approved")

        print("5. CALLABLE — through the policy layer, audited")
        ok, _ = anyio.run(call, "echo", "one two  three\nfour")
        check(
            "returns the right answer",
            ok.ok and ok.data == {"words": 4, "lines": 2, "characters": 19},
            str(ok.data),
        )
        check("call audited", call_audit.read_all()[-1].outcome == "ok")

        print("6. EDIT ONE LINE — back to quarantine")
        adapter = folder / "adapter.py"
        adapter.write_text(
            adapter.read_text().replace("len(text)}", "len(text) + 1}"), encoding="utf-8"
        )
        check("status is quarantined again", status() == "quarantined")
        again, _ = anyio.run(call, "echo", "a b")
        check("call refused again", again.error_code == "denied")
        check("grant withdrawn", granted_tool_names("echo") == frozenset())
    finally:
        if not keep and folder.exists():
            _to_recycle_bin(folder)  # Recycle Bin, never a permanent delete
    failed = [label for label, ok in CHECKS if not ok]
    print(f"\n{'PASS' if not failed else 'FAIL'}: {len(CHECKS) - len(failed)}/{len(CHECKS)} checks")
    print(f"audit + approval records: {work}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
