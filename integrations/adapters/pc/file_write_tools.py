"""Write tools for the File & System agent (build step 9; master document 4.3, 9.3, 9.4):
`restore_file` (T3), `move_file` (T3) and `delete_file` (T4).

None of them can run on a model's say-so. The policy layer pauses each call, shows the
owner the `describe_*` text below and continues only on a one-time approval of that exact
call. On top of that, every tool here keeps these rules itself:

- Every path passes `PathPolicy.check(path, "modify")` (rule 10): inside an approved folder
  or the workspace, never a protected or blocked path, symlinks resolved first.
- Files only, one at a time. No folders, no wildcards.
- Nothing is ever overwritten: an existing destination is an error.
- Deleting means the Recycle Bin (`send2trash`), never a permanent delete (rule 9).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from integrations.adapters.pc.file_tools import FileToolDeps
from integrations.adapters.pc.recycle_bin import (
    RecycleBinError,
    RecycleBinUnavailable,
    get_recycle_bin,
    move_to_recycle_bin,
)
from integrations.mcp.tool_schemas import (
    PermissionTier,
    ToolManifest,
    ToolPermissions,
    ToolSpec,
    ToolStatus,
)
from memory.pc_index.index import FileIndex
from memory.pc_index.scanner import entry_for, index_roots, root_of
from security.permissions.path_check import PathDenied, PathPolicy

# Owner approved all three manifests on 2026-11-17 (add-tool skill, section 4). A new or
# changed write tool starts as "quarantined" again until the owner has seen it.
WRITE_TOOLS_STATUS: ToolStatus = "approved"
_WRITE_SCOPE = ToolPermissions(filesystem=["approved_folders", "workspace", "recycle_bin"])


class ToolRefused(RuntimeError):
    """The action cannot be done as asked. The message is safe to show to the agent."""


def _file(deps: FileToolDeps, path: str) -> Path:
    real = deps.policy.check(path, "modify")
    if not real.exists():
        raise ToolRefused(f"there is no file at '{real}'")
    if not real.is_file():
        raise ToolRefused(f"'{real}' is a folder; only single files are supported")
    return real


def _drives(deps: FileToolDeps) -> list[Path]:
    return sorted({Path(r.anchor) for r in index_roots(deps.policy)})


def _reindex(deps: FileToolDeps, gone: Path | None = None, new: Path | None = None) -> None:
    """Keep the PC file index in step right away (the watcher would catch up anyway)."""
    try:
        if gone is not None:
            deps.index.remove(gone)
        if new is not None:
            root = root_of(new, index_roots(deps.policy))
            entry = entry_for(new, root, deps.policy) if root else None
            if entry is not None:
                deps.index.upsert([entry])
    except Exception:  # noqa: BLE001, S110 - the file change is done; the index is only a cache
        pass


def _shown(deps: FileToolDeps, path: str) -> str:
    try:
        return str(deps.policy.check(path, "modify"))
    except (PathDenied, OSError):
        return f"{path}  (HCWMA may not touch this path; the tool will refuse)"


# ---------- move_file ----------


class MoveFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=500, description="Full path of the file.")
    destination: str = Field(
        min_length=1,
        max_length=500,
        description="New full path, or an existing folder.",
    )


class MoveFileOutput(BaseModel):
    moved: bool
    source: str
    destination: str


def _move_target(deps: FileToolDeps, source: Path, destination: str) -> Path:
    target = deps.policy.check(destination, "read")
    if target.is_dir():
        target = target / source.name
    return deps.policy.check(target, "modify")


def _move_file(deps: FileToolDeps, args: MoveFileInput) -> MoveFileOutput:
    source = _file(deps, args.source)
    target = _move_target(deps, source, args.destination)
    if target.exists():
        raise ToolRefused(f"something already exists at '{target}'; nothing was moved")
    if not target.parent.is_dir():
        raise ToolRefused(f"the folder '{target.parent}' does not exist; nothing was moved")
    shutil.move(str(source), str(target))
    _reindex(deps, gone=source, new=target)
    return MoveFileOutput(moved=True, source=str(source), destination=str(target))


def _describe_move(deps: FileToolDeps, args: MoveFileInput) -> str:
    source = _shown(deps, args.source)
    try:
        target = str(_move_target(deps, Path(source), args.destination))
    except (PathDenied, OSError):
        target = _shown(deps, args.destination)
    return f"Move (or rename) the file\n{source}\nto\n{target}\nNothing is overwritten."


# ---------- delete_file ----------


class DeleteFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=500, description="Full path of the file.")


class DeleteFileOutput(BaseModel):
    moved_to_recycle_bin: bool
    path: str
    note: str


def _delete_file(deps: FileToolDeps, args: DeleteFileInput) -> DeleteFileOutput:
    target = _file(deps, args.path)
    move_to_recycle_bin(target)
    _reindex(deps, gone=target)
    return DeleteFileOutput(
        moved_to_recycle_bin=True,
        path=str(target),
        note="The file is in the Recycle Bin and can be restored from there.",
    )


def _describe_delete(deps: FileToolDeps, args: DeleteFileInput) -> str:
    return (
        f"Delete the file\n{_shown(deps, args.path)}\n"
        "It goes to the Recycle Bin and can be restored from there. It is not erased."
    )


# ---------- restore_file ----------


class RestoreFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(
        min_length=4,
        max_length=300,
        description="item_id from check_recycle_bin, copied exactly.",
    )


class RestoreFileOutput(BaseModel):
    restored: bool
    path: str


def _restore_file(deps: FileToolDeps, args: RestoreFileInput) -> RestoreFileOutput:
    try:
        restored = deps.recycle_bin.restore(
            args.item_id, _drives(deps), lambda original: deps.policy.check(original, "modify")
        )
    except (RecycleBinError, RecycleBinUnavailable) as exc:
        raise ToolRefused(str(exc)) from exc
    _reindex(deps, new=restored)
    return RestoreFileOutput(restored=True, path=str(restored))


def _describe_restore(deps: FileToolDeps, args: RestoreFileInput) -> str:
    try:
        item = deps.recycle_bin.find(args.item_id, _drives(deps))
    except RecycleBinUnavailable as exc:
        return f"Restore a file from the Recycle Bin. {exc}; the tool will refuse."
    if item is None:
        return (
            f"Restore Recycle Bin item {args.item_id!r}. It is not in the Recycle Bin, so "
            "the tool will change nothing."
        )
    deleted = item.deleted_at.astimezone().strftime("%Y-%m-%d %H:%M")
    return (
        f"Restore from the Recycle Bin, back to where it was deleted from:\n"
        f"{_shown(deps, item.original_path)}\n(deleted {deleted}). Nothing is overwritten."
    )


# ---------- registration ----------


def file_write_tool_specs(deps: FileToolDeps, status: ToolStatus = "quarantined") -> list[ToolSpec]:
    def manifest(name: str, description: str, tier: PermissionTier) -> ToolManifest:
        return ToolManifest(
            name=name,
            version="0.1.1",  # 0.1.1: shorter descriptions (2026-11-19)
            created_by="developer",
            status=status,
            route="pc",
            description=description,
            permission_tier=tier,
            approval_required=True,
            side_effects="writes",
            permissions=_WRITE_SCOPE,
            timeout_s=30,
        )

    return [
        ToolSpec(
            manifest=manifest(
                "restore_file",
                "Restore one file from the Recycle Bin. Needs item_id from check_recycle_bin. "
                "Owner approves first. Never overwrites.",
                PermissionTier.T3,
            ),
            input_model=RestoreFileInput,
            output_model=RestoreFileOutput,
            handler=lambda args: _restore_file(deps, args),
            describe=lambda args: _describe_restore(deps, args),
        ),
        ToolSpec(
            manifest=manifest(
                "move_file",
                "Move or rename one file (not a folder). Owner approves first. Never overwrites.",
                PermissionTier.T3,
            ),
            input_model=MoveFileInput,
            output_model=MoveFileOutput,
            handler=lambda args: _move_file(deps, args),
            describe=lambda args: _describe_move(deps, args),
        ),
        ToolSpec(
            manifest=manifest(
                "delete_file",
                "Move one file (not a folder) to the Recycle Bin. Owner approves first.",
                PermissionTier.T4,
            ),
            input_model=DeleteFileInput,
            output_model=DeleteFileOutput,
            handler=lambda args: _delete_file(deps, args),
            describe=lambda args: _describe_delete(deps, args),
        ),
    ]


def default_file_write_tool_specs() -> list[ToolSpec]:
    """The write tools wired to the real index, `configs/permissions.yaml` and this OS."""
    deps = FileToolDeps(
        index=FileIndex(), policy=PathPolicy.from_config(), recycle_bin=get_recycle_bin()
    )
    return file_write_tool_specs(deps, status=WRITE_TOOLS_STATUS)
