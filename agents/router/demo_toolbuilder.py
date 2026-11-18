"""Step 13 "Done when" check with the REAL model, run by hand (needs Ollama and Windows Sandbox):

    python -m agents.router.demo_toolbuilder [--keep]

The owner asks for something no tool covers ("a tool that counts the words in a text"). The
real Router sends it to the Tool Builder (Qwen), which looks for an existing tool, writes the
manifest, code and tests, has them run in a real Windows Sandbox and submits the tool. Then
the checks: the tool is quarantined and cannot be called; after the owner's approval it works;
the audit log shows every step. Nothing the model wrote runs on this PC before approval.

Unlike `python -m integrations.generated_tools.proof` (a scripted tool), here the model writes
the code, so it can fail: a 4B model may need more than one attempt. The result is reported
as it is. The tool folder goes to the Recycle Bin at the end unless `--keep`. Approval state
goes to a fresh folder in `workspace/`. About 10-20 minutes on this CPU.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

import anyio
from mcp import Client

from agents.router.factory import open_router
from agents.runtime.policy import PolicyLayer
from integrations.adapters.local_data.tool_builder_tools import (
    ToolBuilderDeps,
    tool_builder_specs,
)
from integrations.generated_tools.manifest import REPO_ROOT, generated_root
from integrations.generated_tools.registry import granted_tool_names
from integrations.generated_tools.review import ReviewService
from integrations.generated_tools.store import GeneratedStore, _to_recycle_bin, read_tool
from integrations.mcp.client import McpToolClient, local_server_params
from integrations.mcp.tool_schemas import PermissionTier
from integrations.registry.capability_catalog import CapabilityCatalog, default_catalog
from model.serving import Message
from security.audit.audit_logger import AuditLogger, get_audit_logger
from security.permissions.permission_manager import (
    AgentGrant,
    PermissionManager,
    load_permissions_config,
)

REQUEST = (
    "I need a new tool called word_count. It should take a text and return the number of "
    "words and the number of lines. Nothing else exists for this, so please build it."
)
CHECKS: list[tuple[str, bool]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((label, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))


async def call_tool(name: str, grant: AgentGrant, args: dict[str, Any], audit: AuditLogger) -> Any:
    async with McpToolClient(Client(local_server_params())) as client:
        policy = PolicyLayer(
            client=client,
            catalog=default_catalog(),
            permissions=PermissionManager(load_permissions_config()),
            audit=audit,
        )
        return await policy.call_tool("demo", grant, name, args)


async def run(keep: bool) -> bool:
    audit = get_audit_logger()
    before = len(audit.read_all())
    work = REPO_ROOT / "workspace" / f"step13_demo_{time.strftime('%Y%m%d_%H%M%S')}"
    work.mkdir(parents=True)
    os.environ["HCWMA_DATA_DIR"] = str(work / "data")
    root = generated_root()
    if (root / "word_count").exists():
        print("integrations/generated/word_count already exists; move it away first.")
        return False

    catalog = CapabilityCatalog()
    for spec in tool_builder_specs(ToolBuilderDeps(), status="approved"):
        catalog.register(spec)
    folder = root / "word_count"
    try:
        print(f"\n> {REQUEST}")
        async with open_router(catalog=catalog, with_memory=False) as router:
            reply = await router.handle([Message(role="user", content=REQUEST)])
        print(f"  [router -> {' -> '.join(reply.agents)} | {reply.action} | {reply.status}]")
        for line in reply.reply.strip().splitlines():
            print(f"  {line}")

        tool = read_tool("word_count")
        store = GeneratedStore()
        state = store.state("word_count")
        records = [r for r in audit.read_all()[before:] if r.event == "generated_tool"]
        calls = [r for r in audit.read_all()[before:] if r.event == "tool_call"]
        print("\n--- audit (this run) ---")
        for rec in [*calls, *records]:
            print(f"  {rec.agent} -> {rec.tool} {rec.args.get('action', '')} [{rec.outcome}]")

        check("Router sent it to the Tool Builder", reply.agents == ["tool_builder"])
        check("the model wrote the three files", tool is not None and tool.ok)
        if tool is None or not tool.ok:
            print("\n  The model did not produce a valid tool. Problems:")
            for problem in tool.problems if tool else ["no folder"]:
                print(f"   - {problem}")
            return False
        status, _ = store.status_of(tool)
        check("tool is quarantined", status == "quarantined")
        check(
            "sandbox tests passed for these exact files",
            bool(
                state.tested and state.tested.get("passed") and state.tested["hash"] == tool.digest
            ),
        )
        check("submitted for review", bool(state.submitted))
        check("sandbox run audited", any(r.args.get("action") == "sandbox_test" for r in records))
        grant = AgentGrant(
            agent_id="echo", tools=frozenset({"word_count"}), max_tier=PermissionTier.T2
        )
        denied = await call_tool("word_count", grant, {"text": "a b"}, audit)
        check("cannot be called while quarantined", denied.error_code == "denied")

        owner = ReviewService(store, audit, ["echo", "file_system"])
        review = owner.review("word_count")
        print("\n--- what the owner sees ---")
        print(review["summary"])
        print(review["adapter_code"])
        owner.approve("word_count", review["hash"], ["echo"])
        grant = AgentGrant(
            agent_id="echo", tools=granted_tool_names("echo"), max_tier=PermissionTier.T2
        )
        fields = tool.manifest.inputs if tool.manifest else {}
        args = {k: "one two  three\nfour" for k in fields}
        result = await call_tool("word_count", grant, args, audit)
        print(f"\ncall after approval: {result.data or result.error}")
        check("callable after approval", result.ok)
        values = list((result.data or {}).values())
        check("the answer is right (4 words)", 4 in values)
        adapter = folder / "adapter.py"
        adapter.write_text(adapter.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
        again = await call_tool("word_count", grant, args, audit)
        check("one edited line -> quarantined, uncallable", again.error_code == "denied")
    finally:
        if not keep and folder.exists():
            _to_recycle_bin(folder)
    failed = [label for label, ok in CHECKS if not ok]
    print(f"\n{'PASS' if not failed else 'FAIL'}: {len(CHECKS) - len(failed)}/{len(CHECKS)} checks")
    return not failed


def main() -> int:
    return 0 if anyio.run(run, "--keep" in sys.argv) else 1


if __name__ == "__main__":
    sys.exit(main())
