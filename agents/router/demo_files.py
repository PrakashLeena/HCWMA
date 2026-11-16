"""Step 8 "Done when" check, run by hand (needs Ollama running):

    python -m agents.router.demo_files

Part 1 — the real stack (real Router and agent on Qwen, MCP server as a child process,
`configs/permissions.yaml` as it is). No folder is approved yet, so the files live in a
demo folder inside the HCWMA workspace, `workspace/step8_demo/`:

1. "I'm missing one of my project files, it was called annek-proposal" -> found, with path.
2. The same for a file that does not exist -> the agent says it cannot find it.

Part 2 — a protected path. A throw-away "PC" under `memory/pc_index/data/demo_pc/` (not in
Git) has an approved folder with a protected sub-folder holding `annek-passwords.txt`. Same
Router, agent and tools on Qwen, in-process MCP server. The protected file must not be in
the index, in any tool result, or in the reply.

At the end every tool call made is shown from the audit log.
"""

from __future__ import annotations

import re
from pathlib import Path

import anyio

from agents.router.factory import open_router
from agents.router.router import Router, RouterReply
from integrations.adapters.pc.file_tools import (
    FileToolDeps,
    SearchFilesInput,
    file_tool_specs,
)
from integrations.adapters.pc.recycle_bin import get_recycle_bin
from integrations.registry.capability_catalog import CapabilityCatalog, default_catalog
from memory.pc_index.index import DEFAULT_INDEX_PATH, FileIndex
from memory.pc_index.scanner import scan
from model.serving import Message
from security.audit.audit_logger import get_audit_logger
from security.permissions.path_check import PathPolicy

FOUND_NAME = "annek-proposal-draft.docx"
SECRET_NAME = "annek-passwords.txt"  # noqa: S105 - a decoy file name, not a secret
DEMO_PC = DEFAULT_INDEX_PATH.parent / "demo_pc"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(text, encoding="utf-8")
    return path


def prepare_workspace(policy: PathPolicy) -> Path:
    demo = policy.workspace / "step8_demo"
    _write(demo / "projects" / FOUND_NAME, "Demo file for build step 8.\n")
    _write(demo / "projects" / "budget-2026.xlsx", "Demo file for build step 8.\n")
    _write(demo / "notes" / "meeting-notes.txt", "Demo file for build step 8.\n")
    return demo


def prepare_demo_pc() -> tuple[PathPolicy, FileIndex, Path]:
    docs = DEMO_PC / "docs"
    _write(docs / "projects" / "annek-proposal-final.docx", "Demo file for build step 8.\n")
    secret = _write(docs / "private" / SECRET_NAME, "Decoy for build step 8. Not a real secret.\n")
    policy = PathPolicy(
        repo_root=DEMO_PC,
        home=DEMO_PC / "home",
        workspace="workspace",
        approved_folders=[str(docs)],
        protected_paths=[str(docs / "private")],
        blocked_folders=[],
    )
    return policy, FileIndex(DEMO_PC / "index.sqlite"), secret


async def ask(router: Router, text: str) -> RouterReply:
    print(f"\n> {text}")
    reply = await router.handle([Message(role="user", content=text)])
    path = " -> ".join(["router", *reply.agents])
    stopped = f" | stopped by {reply.stopped_by}" if reply.stopped_by else ""
    print(f"  [{path} | {reply.action} | {reply.status} | tool calls {reply.tool_calls}{stopped}]")
    for line in reply.reply.strip().splitlines():
        print(f"  {line}")
    return reply


def _names_a_file(reply: str, word: str) -> bool:
    """True if the reply contains a path-like token with `word` in it (an invented result)."""
    return bool(re.search(rf"[\\/][^\s\\/]*{re.escape(word)}[^\s\\/]*\.\w{{2,5}}\b", reply, re.I))


async def run() -> bool:
    audit = get_audit_logger()
    before = len(audit.read_all())

    statuses = {s.name: s.manifest.status for s in default_catalog().all_tools()}
    if any(statuses.get(t) != "approved" for t in ("search_files", "check_recycle_bin")):
        print("The file tools are still quarantined: the owner has not approved their manifests.")
        return False

    print("--- part 1: real stack, demo folder in the workspace ---")
    real_policy = PathPolicy.from_config()
    demo = prepare_workspace(real_policy)
    report = scan(FileIndex(), real_policy)
    print(f"indexed {report.files} files in: {', '.join(report.roots) or '(nothing)'}")

    async with open_router() as router:
        found = await ask(
            router, "I'm missing one of my project files, it was called annek-proposal"
        )
        gone = await ask(router, "I'm missing one of my project files, it was called zebra-invoice")

    print("\n--- part 2: a protected folder inside an approved folder ---")
    policy, index, secret = prepare_demo_pc()
    scan(index, policy)
    deps = FileToolDeps(index=index, policy=policy, recycle_bin=get_recycle_bin())
    catalog = CapabilityCatalog()
    specs = file_tool_specs(deps, status="approved")
    for spec in specs:
        catalog.register(spec)
    print(f"protected file on disk: {secret} (exists: {secret.exists()})")
    in_index = [p for p, _ in index.sizes(DEMO_PC) if SECRET_NAME in p]
    direct = specs[0].handler(SearchFilesInput(query="annek passwords", limit=50))
    direct_paths = [hit.path for hit in direct.results]
    print(f"rows for it in the index: {len(in_index)}")
    print(f"search_files('annek passwords') returned: {direct_paths or 'nothing'}")

    async with open_router(catalog=catalog) as router:
        hidden = await ask(router, "I lost a file called annek-passwords, please find it")
        both = await ask(router, "Find every file with annek in its name")

    new = audit.read_all()[before:]
    tool_records = [r for r in new if r.event == "tool_call"]
    print(f"\n--- audit log: {audit.path} ---")
    for rec in tool_records:
        print(f"  {rec.ts}  {rec.agent} -> {rec.tool} {rec.args}  [{rec.outcome}]")
    replies = [found, gone, hidden, both]
    made = sum(r.tool_calls for r in replies)

    checks = {
        "router -> file_system for the missing-file request": found.agents == ["file_system"],
        "the missing file is found, with its path": found.status == "done"
        and str(demo / "projects" / FOUND_NAME).lower() in found.reply.lower(),
        "a file that does not exist: searched, answered, nothing invented": gone.agents
        == ["file_system"]
        and gone.status == "done"
        and gone.tool_calls >= 1
        and not _names_a_file(gone.reply, "zebra"),
        "every tool call is in the audit log": made >= 3 and len(tool_records) == made,
        "audit records name the agent and the tool": all(
            r.agent == "file_system" and r.tool for r in tool_records
        ),
        "protected file is not in the index": not in_index,
        "protected file is not in a direct tool result": not any(
            SECRET_NAME in p for p in direct_paths
        ),
        "protected file is not in any reply": not any(SECRET_NAME in r.reply for r in replies)
        and hidden.agents == ["file_system"]
        and hidden.tool_calls >= 1,
        "the ordinary file next to it is still found": "annek-proposal-final.docx" in both.reply,
    }
    print()
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    passed = all(checks.values())
    print("\nSTEP 8 DONE-WHEN:", "PASS" if passed else "FAIL")
    return passed


def main() -> None:
    raise SystemExit(0 if anyio.run(run) else 1)


if __name__ == "__main__":
    main()
