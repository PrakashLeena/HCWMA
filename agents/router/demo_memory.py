"""Step 10 "Done when" check, run by hand (needs Ollama running, ~5-10 min on CPU):

    python -m agents.router.demo_memory

The real stack on Qwen: Router, Memory Librarian, MCP server as a child process, the real
policy layer and approval service. Memory goes to a demo data folder in
`workspace/step10_demo/` (not the owner's real memory). This script plays the owner where
the desktop app would show Approve / Deny.

1. Session 1: "Remember that ..." -> the Librarian stores it.
2. Restart: a new Router, a new tool server process, a new memory service on the same
   folder, a new chat. "When is ...?" -> recalled.
3. Scopes: an agent is refused a memory scope that is not on its card.
4. Backup, then restore into a clean folder -> the same entries.
5. "Forget ..." -> the owner approves the prompt -> gone from retrieval, the full-text
   index and the database file; asking again no longer gives the answer.
"""

from __future__ import annotations

import os
import time

import anyio
from send2trash import send2trash

from integrations.mcp.tool_schemas import CallerContext
from integrations.registry.capability_catalog import default_catalog
from memory.config import DATA_DIR_ENV, MemoryConfig, memory_db_path
from memory.service import MemoryService, ScopeDenied
from memory.storage.store import MemoryStore
from security.audit.audit_logger import get_audit_logger
from security.permissions.path_check import PathPolicy

FACT_WORDS = ("12", "march")
TOOLS = ("memory_search", "memory_write", "memory_delete", "memory_forget_all", "reindex_folder")


def _show(reply) -> None:  # type: ignore[no-untyped-def]
    path = " -> ".join(["router", *reply.agents])
    print(f"  [{path} | {reply.action} | {reply.status} | tool calls {reply.tool_calls}]")
    for line in reply.reply.strip().splitlines():
        print(f"  {line}")


async def ask(router, text: str, owner: bool | None = None):  # type: ignore[no-untyped-def]
    from model.serving import Message

    print(f"\n> {text}")
    prompts = []
    start = time.perf_counter()

    async def play_owner() -> None:
        while not router.approvals.pending():
            await anyio.sleep(0.1)
        prompt = router.approvals.pending()[0]
        prompts.append(prompt)
        print(
            f"  --- APPROVAL PROMPT ({prompt.agent} -> {prompt.tool}, tier {prompt.tier.value}) ---"
        )
        for line in prompt.summary.splitlines():
            print(f"  | {line}")
        print(f"  --- the owner clicks {'APPROVE' if owner else 'DENY'} ---")
        router.approvals.decide(prompt.id, bool(owner))

    async with anyio.create_task_group() as tg:
        if owner is not None:
            tg.start_soon(play_owner)
        reply = await router.handle([Message(role="user", content=text)])
        tg.cancel_scope.cancel()
    print(f"  ({time.perf_counter() - start:.0f} s)")
    _show(reply)
    return reply, prompts


def _mentions_fact(text: str) -> bool:
    low = text.lower()
    return all(w in low for w in FACT_WORDS)


async def run() -> bool:
    from agents.router.factory import open_router

    statuses = {s.name: s.manifest.status for s in default_catalog().all_tools()}
    if any(statuses.get(t) != "approved" for t in TOOLS):
        print("The memory tools are still quarantined: the owner has not approved their manifests.")
        return False

    demo = PathPolicy.from_config().workspace / "step10_demo"
    data, clean = demo / "data", demo / "clean_pc"
    for folder in (data, clean):
        if folder.exists():
            send2trash(str(folder))  # an earlier run's demo folder: to the Recycle Bin
    os.environ[DATA_DIR_ENV] = str(data)  # also inherited by the MCP server child process
    audit = get_audit_logger()
    before = len(audit.read_all())
    print(f"setup: demo memory folder {data} (empty)")

    print("\n--- 1. session 1: tell HCWMA a fact ---")
    async with open_router() as router:
        told, _ = await ask(router, "Remember that my sister's birthday is on 12 March.")
    stored = MemoryService.from_config().everything()
    for e in stored:
        print(f"  memory: [{e.scope}] {e.text}  ({e.source})")

    print("\n--- 2. restart: new Router, new tool server, new memory service, new chat ---")
    async with open_router() as router:
        recalled, _ = await ask(router, "When is my sister's birthday?")

        print("\n--- 3. scopes: an agent cannot read a memory scope that is not on its card ---")
        registry = router._runtime.registry  # demo only: reach the live registry
        file_card = registry.get("file_system")
        service = MemoryService.from_config()
        try:
            service.retrieve(
                "sister", agent_id="file_system", allowed_scopes=file_card.memory_scopes,
                scopes=["summaries"],
            )  # fmt: skip
            scope_refused = False
        except ScopeDenied as exc:
            print(f"  memory service: {exc}")
            scope_refused = True
        gateway = router._runtime._gateway
        via_tool = await gateway.call_tool(
            "demo-scope",
            file_card.grant(),
            "memory_search",
            {"query": "sister"},
            context=CallerContext(
                agent_id="file_system", task_id="demo-scope",
                memory_scopes=tuple(file_card.memory_scopes),
            ),
        )  # fmt: skip
        print(f"  file_system -> memory_search: {via_tool.error_code}: {via_tool.error}")

        print("\n--- 4. backup, then restore on a clean folder ---")
        snapshot = sorted(e.model_dump_json() for e in service.everything())
        backup = service.backup()
        fresh = MemoryService(
            MemoryStore(memory_db_path(clean)), MemoryConfig(), backup_dir=clean / "backups"
        )
        restored_count = fresh.restore(backup)
        same = sorted(e.model_dump_json() for e in fresh.everything()) == snapshot
        print(
            f"  backup {backup.name}: restored {restored_count} entries into {clean}; same: {same}"
        )

        print("\n--- 5. forget it (the owner approves) ---")
        forgot, prompts = await ask(router, "Forget my sister's birthday.", owner=True)
        again, _ = await ask(router, "When is my sister's birthday?")

    after = MemoryService.from_config()
    db_bytes = b"".join(p.read_bytes() for p in after.store.path.parent.iterdir() if p.is_file())
    new = audit.read_all()[before:]
    print(f"\n--- audit log: {audit.path} (memory, approvals, tool calls) ---")
    for rec in new:
        if rec.event in ("memory", "approval", "tool_call"):
            print(f"  {rec.ts}  {rec.event:9}  {rec.agent} -> {rec.tool}  [{rec.outcome}]")

    checks = {
        "remember: the Librarian stored the fact as the owner's words": told.agents
        == ["memory_librarian"]
        and any(e.scope == "profile" and _mentions_fact(e.text) for e in stored),
        "recall: a new session after a restart answers from memory": _mentions_fact(recalled.reply),
        "scopes: the memory service refuses a scope not on the card": scope_refused,
        "scopes: an agent without the memory tools is denied": via_tool.error_code == "denied",
        "backup + restore on a clean folder gives the same entries": same and restored_count > 0,
        "forget: the owner was asked first, with the entry shown": len(prompts) == 1
        and prompts[0].tool == "memory_delete"
        and _mentions_fact(prompts[0].summary),
        "forget: gone from retrieval, the full-text index and the file": not any(
            _mentions_fact(e.text) for e in after.everything()
        )
        and not after.store.in_full_text_index("sister")
        and b"sister" not in db_bytes.lower(),
        "forget: asking again no longer gives the date": not _mentions_fact(again.reply),
        "memory writes and the delete are in the audit log": any(
            r.event == "memory" and r.outcome == "stored" for r in new
        )
        and any(r.event == "memory" and r.tool == "forget" for r in new),
    }
    print()
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    passed = all(checks.values())
    print("\nSTEP 10 DONE-WHEN:", "PASS" if passed else "FAIL")
    return passed


def main() -> None:
    raise SystemExit(0 if anyio.run(run) else 1)


if __name__ == "__main__":
    main()
