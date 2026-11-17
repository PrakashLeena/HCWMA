"""Memory tools for the Memory (Librarian) agent (build step 10; master document 4.3, 8;
`docs/memory-policy.md`).

| tool                | tier | what it does                                                  |
|---------------------|------|---------------------------------------------------------------|
| `memory_search`     | T0   | search or list memory in the caller's card scopes              |
| `memory_write`      | T2   | "remember that ...": store the owner's own words               |
| `memory_delete`     | T3   | "forget that": delete entries, after the owner approves        |
| `memory_forget_all` | T4   | "forget everything": one-time owner approval, like any T4 act  |
| `reindex_folder`    | T1   | refresh the PC file index for one indexed folder (or all)      |

Every tool gets a `CallerContext` from the policy layer: the calling agent's memory scopes
(from its card) and the owner's latest message (for grounding). Neither can come from the
model. `memory_write` can only store what the owner typed (`owner_said`, grounded), so text
from files, web pages or tool output never becomes a "fact about the owner".

Memory lives in HCWMA's own data folder, not in the owner's files: writing it is T2
("HCWMA's own data"), and "remember that ..." is stored at once (memory policy 4).
Deleting asks the owner first.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from integrations.mcp.tool_schemas import (
    CallerContext,
    PermissionTier,
    ToolManifest,
    ToolPermissions,
    ToolSpec,
    ToolStatus,
)
from memory.pc_index.index import FileIndex
from memory.pc_index.scanner import allowed_dir, index_roots, root_of, scan, scan_tree
from memory.service import MemoryService, ScopeDenied
from memory.storage.store import MemoryEntry
from security.audit.audit_logger import get_audit_logger
from security.permissions.path_check import PathDenied, PathPolicy

# Owner approved all five manifests on 2026-11-17 (add-tool skill, section 4). A new or
# changed memory tool starts as "quarantined" again until the owner has seen it.
MEMORY_TOOLS_STATUS: ToolStatus = "approved"
_MEMORY_SCOPE = ToolPermissions(filesystem=["hcwma_data/memory", "hcwma_data/backups"])
_ID = r"^[0-9a-f]{12}$"


@dataclass(frozen=True)
class MemoryToolDeps:
    """Built lazily, so each process (and each test) opens its own data folder."""

    service: Callable[[], MemoryService]
    index: Callable[[], FileIndex]
    policy: Callable[[], PathPolicy]


class MemoryItem(BaseModel):
    id: str
    scope: str
    text: str
    source: str
    saved: str  # date
    for_agent: str | None = None


def _item(entry: MemoryEntry) -> MemoryItem:
    return MemoryItem(
        id=entry.id,
        scope=entry.scope,
        text=entry.text,
        source=entry.source,
        saved=entry.created_at[:10],
        for_agent=entry.agent,
    )


class ToolRefused(RuntimeError):
    """A clear refusal the agent may see."""


# ---------- memory_search ----------


class MemorySearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        default="", max_length=200, description="Words to look for. Empty: list the newest."
    )
    scopes: list[Literal["profile", "episodic", "summaries", "skill_notes"]] | None = Field(
        default=None, max_length=4, description="Only these layers. Default: all you may read."
    )
    limit: int = Field(default=10, ge=1, le=20)


class MemorySearchOutput(BaseModel):
    count: int
    entries: list[MemoryItem]
    note: str | None = None


def _memory_search(
    deps: MemoryToolDeps, args: MemorySearchInput, caller: CallerContext
) -> MemorySearchOutput:
    try:
        entries = deps.service().search(
            args.query,
            agent_id=caller.agent_id,
            allowed_scopes=caller.memory_scopes,
            scopes=args.scopes,
            limit=args.limit,
        )
    except ScopeDenied as exc:
        raise ToolRefused(str(exc)) from None
    note = None if entries else "Nothing in memory matches. This is the final result."
    return MemorySearchOutput(count=len(entries), entries=[_item(e) for e in entries], note=note)


# ---------- memory_write ----------


class MemoryWriteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(
        min_length=1,
        max_length=500,
        description="One short fact or preference, in the owner's own words.",
    )
    scope: Literal["profile", "skill_notes"] = "profile"
    for_agent: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{0,39}$",
        description="skill_notes only: the agent id the note is for.",
    )


class MemoryWriteOutput(BaseModel):
    status: Literal["stored", "updated", "rejected"]
    id: str | None = None
    stored_text: str | None = None
    reason: str | None = None
    left_out: list[str] = Field(default_factory=list)


_REASONS = {
    "instruction": "it reads like an instruction to HCWMA; memory can only hold facts",
    "blocked": "it is a password, key, card, account or ID number, which HCWMA never stores",
    "not_owner_words": "it is not what the owner said; only the owner's own words are saved",
    "empty": "there was nothing to save",
}


def _memory_write(
    deps: MemoryToolDeps, args: MemoryWriteInput, caller: CallerContext
) -> MemoryWriteOutput:
    if args.scope == "skill_notes" and not args.for_agent:
        raise ToolRefused("a skill note needs 'for_agent'")
    try:
        result = deps.service().write(
            args.text,
            scope=args.scope,
            source="owner_said",
            agent_id=caller.agent_id,
            allowed_scopes=caller.memory_scopes,
            owner_words=caller.owner_words,
            note_for=args.for_agent if args.scope == "skill_notes" else None,
            task_id=caller.task_id,
        )
    except ScopeDenied as exc:
        raise ToolRefused(str(exc)) from None
    if result.entry is None:
        return MemoryWriteOutput(
            status="rejected",
            reason=_REASONS.get(result.reason, result.reason),
            left_out=result.dropped,
        )
    return MemoryWriteOutput(
        status=result.status,
        id=result.entry.id,
        stored_text=result.entry.text,
        left_out=result.dropped,
    )


# ---------- memory_delete ----------


class MemoryDeleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[str] = Field(
        min_length=1,
        max_length=20,
        description="The 'id' values from memory_search, copied exactly.",
    )

    @field_validator("ids")
    @classmethod
    def _ids(cls, ids: list[str]) -> list[str]:
        bad = [i for i in ids if not re.fullmatch(_ID, i)]
        if bad:
            raise ValueError(f"not memory ids: {bad[:3]}")
        return list(dict.fromkeys(ids))


class MemoryDeleteOutput(BaseModel):
    deleted: int
    also_deleted_derived: int
    backups_cleaned: int
    not_found: list[str] = Field(default_factory=list)


def _memory_delete(
    deps: MemoryToolDeps, args: MemoryDeleteInput, caller: CallerContext
) -> MemoryDeleteOutput:
    service = deps.service()
    found = [i for i in args.ids if service.store.get(i) is not None]
    try:
        result = service.forget(
            found,
            agent_id=caller.agent_id,
            allowed_scopes=caller.memory_scopes,
            task_id=caller.task_id,
        )
    except ScopeDenied as exc:
        raise ToolRefused(str(exc)) from None
    return MemoryDeleteOutput(
        deleted=len(result.deleted_ids),
        also_deleted_derived=result.also_deleted_derived,
        backups_cleaned=result.backups_cleaned,
        not_found=[i for i in args.ids if i not in found],
    )


def _describe_delete(deps: MemoryToolDeps, args: MemoryDeleteInput) -> str:
    service = deps.service()
    lines = []
    for entry_id in args.ids:
        entry = service.store.get(entry_id)
        lines.append(
            f"- [{entry.scope}] {entry.text}" if entry else f"- {entry_id} (no such entry)"
        )
    return (
        f"Forget {len(args.ids)} memory entr{'y' if len(args.ids) == 1 else 'ies'} for good:\n"
        + "\n".join(lines)
        + "\nSummaries made from them are removed too, and so are their copies in the "
        "memory backups. This cannot be undone."
    )


# ---------- memory_forget_all ----------


class MemoryForgetAllInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemoryForgetAllOutput(BaseModel):
    deleted: int


def _memory_forget_all(
    deps: MemoryToolDeps, _: MemoryForgetAllInput, caller: CallerContext
) -> MemoryForgetAllOutput:
    service = deps.service()
    for scope in ("profile", "episodic", "summaries", "skill_notes"):
        if scope not in caller.memory_scopes:
            raise ToolRefused("only an agent that may read every memory layer can erase them")
    return MemoryForgetAllOutput(
        deleted=service.forget_everything(agent_id=caller.agent_id, task_id=caller.task_id)
    )


def _describe_forget_all(deps: MemoryToolDeps, _: MemoryForgetAllInput) -> str:
    service = deps.service()
    return (
        f"Erase ALL of HCWMA's memory: {service.store.count()} entries (everything it "
        f"remembers about you, past tasks, session summaries and skill notes), and the same "
        f"in all {len(service.backups())} memory backups. This cannot be undone. "
        "Consider `python -m memory export` first."
    )


# ---------- reindex_folder ----------


class ReindexFolderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    folder: str | None = Field(
        default=None,
        max_length=500,
        description="A folder inside an indexed folder. Leave out to refresh everything.",
    )


class ReindexFolderOutput(BaseModel):
    folders: list[str]
    files_seen: int
    changed: int


def _reindex_folder(
    deps: MemoryToolDeps, args: ReindexFolderInput, _: CallerContext
) -> ReindexFolderOutput:
    policy, index = deps.policy(), deps.index()
    if args.folder is None:
        report = scan(index, policy)
        return ReindexFolderOutput(
            folders=report.roots, files_seen=report.files, changed=report.changed + report.removed
        )
    try:
        folder = policy.check(args.folder, "read")
    except PathDenied as exc:
        raise ToolRefused(str(exc)) from None
    root = root_of(folder, index_roots(policy))
    if root is None or not folder.is_dir() or not allowed_dir(folder, policy):
        raise ToolRefused(f"'{folder}' is not a folder HCWMA indexes")
    seen, changed = scan_tree(index, policy, folder, root)
    return ReindexFolderOutput(folders=[str(folder)], files_seen=seen, changed=changed)


# ---------- registration ----------


def _manifest(name: str, description: str, tier: PermissionTier, **kw: object) -> ToolManifest:
    return ToolManifest(
        name=name,
        version="0.1.0",
        created_by="developer",
        route="local_data",
        description=description,
        permission_tier=tier,
        **{"permissions": _MEMORY_SCOPE, **kw},  # type: ignore[arg-type]
    )


def memory_tool_specs(
    deps: MemoryToolDeps, status: ToolStatus = MEMORY_TOOLS_STATUS
) -> list[ToolSpec]:
    def bound(fn: Callable[..., BaseModel]) -> Callable[..., BaseModel]:
        def handler(args: BaseModel, caller: CallerContext) -> BaseModel:
            try:
                return fn(deps, args, caller)
            except ToolRefused as exc:
                raise RuntimeError(str(exc)) from None

        return handler

    return [
        ToolSpec(
            manifest=_manifest(
                "memory_search",
                "Search what HCWMA remembers (or list the newest entries when the query is "
                "empty), in the memory layers this agent may read.",
                PermissionTier.T0,
                status=status,
                timeout_s=10,
            ),
            input_model=MemorySearchInput,
            output_model=MemorySearchOutput,
            handler=bound(_memory_search),
            caller_context=True,
        ),
        ToolSpec(
            manifest=_manifest(
                "memory_write",
                "Remember one short fact or preference the owner just stated, in the owner's "
                "own words. Passwords, keys, card and ID numbers are never stored.",
                PermissionTier.T2,
                status=status,
                side_effects="writes",
                timeout_s=10,
            ),
            input_model=MemoryWriteInput,
            output_model=MemoryWriteOutput,
            handler=bound(_memory_write),
            caller_context=True,
        ),
        ToolSpec(
            manifest=_manifest(
                "memory_delete",
                "Forget memory entries by id (and what was derived from them). The owner "
                "approves each deletion first.",
                PermissionTier.T3,
                status=status,
                approval_required=True,
                side_effects="writes",
                timeout_s=60,
            ),
            input_model=MemoryDeleteInput,
            output_model=MemoryDeleteOutput,
            handler=bound(_memory_delete),
            describe=lambda a: _describe_delete(deps, a),
            caller_context=True,
        ),
        ToolSpec(
            manifest=_manifest(
                "memory_forget_all",
                "Erase everything HCWMA remembers, including memory backups. T4: the owner "
                "must approve this exact action once.",
                PermissionTier.T4,
                status=status,
                approval_required=True,
                side_effects="writes",
                timeout_s=120,
            ),
            input_model=MemoryForgetAllInput,
            output_model=MemoryForgetAllOutput,
            handler=bound(_memory_forget_all),
            describe=lambda a: _describe_forget_all(deps, a),
            caller_context=True,
        ),
        ToolSpec(
            manifest=_manifest(
                "reindex_folder",
                "Refresh HCWMA's PC file index (names, dates, sizes) for one indexed folder, "
                "or for all of them.",
                PermissionTier.T1,
                status=status,
                side_effects="writes",
                permissions=ToolPermissions(filesystem=["approved_folders", "workspace"]),
                timeout_s=120,
            ),
            input_model=ReindexFolderInput,
            output_model=ReindexFolderOutput,
            handler=bound(_reindex_folder),
            caller_context=True,
        ),
    ]


def default_memory_tool_specs() -> list[ToolSpec]:
    """Wired to the real memory folder, PC file index and `configs/permissions.yaml`."""
    return memory_tool_specs(
        MemoryToolDeps(
            service=lambda: MemoryService.from_config(audit=get_audit_logger()),
            index=FileIndex,
            policy=PathPolicy.from_config,
        )
    )


__all__ = ["MemoryToolDeps", "default_memory_tool_specs", "memory_tool_specs"]
