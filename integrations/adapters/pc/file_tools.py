"""Read-only file tools for the File & System agent (build step 8; master document 4.3,
4.6, 6.5, 9.4): `search_files`, `list_recent_files`, `check_recycle_bin`, `get_disk_usage`.

None of them opens a file, writes, moves or deletes anything. Three rules hold for all four:

- Every path that goes in or comes out passes `PathPolicy.check(path, "read")` (rule 10),
  so results only ever name files inside approved folders or the workspace, and never a
  protected path — even if the index were stale or tampered with.
- Results are small on purpose (default 10 rows): the local model reads them on a CPU.
- "Nothing found" and "no folder is indexed" are normal, explicit results, so the agent
  can say so honestly instead of guessing.
"""

from __future__ import annotations

import os
import shutil
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

import psutil
from pydantic import BaseModel, ConfigDict, Field

from integrations.adapters.pc.recycle_bin import (
    RecycleBin,
    RecycleBinUnavailable,
    get_recycle_bin,
)
from integrations.mcp.tool_schemas import (
    PermissionTier,
    ToolManifest,
    ToolPermissions,
    ToolSpec,
    ToolStatus,
)
from memory.pc_index.index import FileEntry, FileIndex, query_terms
from memory.pc_index.scanner import META_LAST_SCAN, index_roots
from security.permissions.path_check import PathDenied, PathPolicy

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
NO_FOLDERS_NOTE = (
    "No folder is indexed: the owner has not approved any folder for HCWMA yet, so files "
    "on this PC cannot be searched."
)
NEVER_SCANNED_NOTE = "The file index has not been built yet, so nothing can be found in it."
NOT_FOUND_NOTE = (
    "No indexed file matches these words. This result is final: searching again with the "
    "same words or with an extension added will not find it. The file may be deleted, "
    "renamed or outside the indexed folders."
)
# Owner approved all four manifests on 2026-11-17 (add-tool skill, section 4). A new or
# changed file tool starts as "quarantined" again until the owner has seen it.
FILE_TOOLS_STATUS: ToolStatus = "approved"
_READ_SCOPE = ToolPermissions(filesystem=["approved_folders", "workspace"])


@dataclass(frozen=True)
class FileToolDeps:
    index: FileIndex
    policy: PathPolicy
    recycle_bin: RecycleBin


# ---------- shared pieces ----------


class FileHit(BaseModel):
    path: str
    modified: str  # local time, "YYYY-MM-DD HH:MM"
    size_bytes: int


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _roots(deps: FileToolDeps) -> list[Path]:
    return index_roots(deps.policy)


def _folders(deps: FileToolDeps, requested: Sequence[str] | None) -> list[Path]:
    """The folders to look in. A requested folder outside policy is an error, not a skip."""
    if not requested:
        return _roots(deps)
    return [deps.policy.check(folder, "read") for folder in requested]


def _visible(deps: FileToolDeps, entries: Sequence[FileEntry], limit: int) -> list[FileHit]:
    """Drop anything policy would refuse today or that no longer exists."""
    hits: list[FileHit] = []
    for entry in entries:
        try:
            real = deps.policy.check(entry.path, "read")
        except (PathDenied, OSError):
            continue
        if not real.is_file():
            continue
        hits.append(FileHit(path=entry.path, modified=_stamp(entry.mtime), size_bytes=entry.size))
        if len(hits) == limit:
            break
    return hits


def _index_note(deps: FileToolDeps) -> str | None:
    if not _roots(deps):
        return NO_FOLDERS_NOTE
    if deps.index.get_meta(META_LAST_SCAN) is None:
        return NEVER_SCANNED_NOTE
    return None


# ---------- search_files ----------


class SearchFilesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        min_length=1,
        max_length=200,
        description="Words from the file name, e.g. 'annek proposal'. Not a full sentence.",
    )
    roots: list[str] | None = Field(
        default=None, max_length=10, description="Only look inside these folders (optional)."
    )
    extension: str | None = Field(
        default=None, max_length=15, pattern=r"^\.?[A-Za-z0-9]+$", description="e.g. 'docx'"
    )
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)


class SearchFilesOutput(BaseModel):
    results: list[FileHit]
    count: int
    match: Literal["all_words", "some_words", "none"]
    more_available: bool
    searched_folders: list[str]
    note: str | None = None


def _search_files(deps: FileToolDeps, args: SearchFilesInput) -> SearchFilesOutput:
    folders = _folders(deps, args.roots)
    terms = query_terms(args.query)
    ext = args.extension
    # A query that is only an extension ("docx", ".pdf") is an extension filter.
    if ext is None and len(terms) == 1 and args.query.strip().startswith("."):
        ext, terms = terms[0], []

    fetch = args.limit * 3 + 1
    match: Literal["all_words", "some_words", "none"] = "all_words"
    found = deps.index.search(terms, folders=folders, ext=ext, limit=fetch)
    if not found and len(terms) > 1:
        found = deps.index.search(terms, folders=folders, ext=ext, limit=fetch, any_term=True)
        match = "some_words"
    hits = _visible(deps, found, args.limit)
    note = _index_note(deps)
    if not hits:
        match = "none"
        note = note or NOT_FOUND_NOTE
    elif match == "some_words":
        note = "No file matches every word; these match at least one."
    return SearchFilesOutput(
        results=hits,
        count=len(hits),
        match=match,
        more_available=len(found) > len(hits) and len(hits) == args.limit,
        searched_folders=[str(f) for f in folders],
        note=note,
    )


# ---------- list_recent_files ----------


class ListRecentFilesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: int = Field(default=7, ge=1, le=365, description="Changed within this many days.")
    extension: str | None = Field(default=None, max_length=15, pattern=r"^\.?[A-Za-z0-9]+$")
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)


class ListRecentFilesOutput(BaseModel):
    results: list[FileHit]  # newest first
    count: int
    days: int
    more_available: bool
    searched_folders: list[str]
    note: str | None = None


def _list_recent_files(deps: FileToolDeps, args: ListRecentFilesInput) -> ListRecentFilesOutput:
    folders = _roots(deps)
    since = time.time() - args.days * 86_400
    found = deps.index.recent(since, folders=folders, ext=args.extension, limit=args.limit * 3 + 1)
    hits = _visible(deps, found, args.limit)
    note = _index_note(deps)
    if not hits and note is None:
        note = f"No indexed file was changed in the last {args.days} day(s)."
    return ListRecentFilesOutput(
        results=hits,
        count=len(hits),
        days=args.days,
        more_available=len(found) > len(hits) and len(hits) == args.limit,
        searched_folders=[str(f) for f in folders],
        note=note,
    )


# ---------- check_recycle_bin ----------


class CheckRecycleBinInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        default="",
        max_length=200,
        description="Words from the deleted file's name. Empty lists the newest deletions.",
    )
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)


class RecycledHit(BaseModel):
    item_id: str
    original_path: str
    deleted: str  # local time, "YYYY-MM-DD HH:MM"
    size_bytes: int


class CheckRecycleBinOutput(BaseModel):
    results: list[RecycledHit]  # newest deletion first
    count: int
    supported: bool
    note: str | None = None


def _check_recycle_bin(deps: FileToolDeps, args: CheckRecycleBinInput) -> CheckRecycleBinOutput:
    roots = _roots(deps)
    if not roots:
        return CheckRecycleBinOutput(results=[], count=0, supported=True, note=NO_FOLDERS_NOTE)
    drives = sorted({Path(r.anchor) for r in roots})
    try:
        items = deps.recycle_bin.list_items(drives)
    except RecycleBinUnavailable as exc:
        return CheckRecycleBinOutput(results=[], count=0, supported=False, note=str(exc))

    terms = query_terms(args.query)
    hits: list[RecycledHit] = []
    for item in sorted(items, key=lambda i: i.deleted_at, reverse=True):
        # Only files that were deleted from a place HCWMA may read are ever shown.
        try:
            deps.policy.check(item.original_path, "read")
        except (PathDenied, OSError):
            continue
        name = os.path.basename(item.original_path).lower()
        if not all(term in name for term in terms):
            continue
        hits.append(
            RecycledHit(
                item_id=item.item_id,
                original_path=item.original_path,
                deleted=item.deleted_at.astimezone().strftime("%Y-%m-%d %H:%M"),
                size_bytes=item.size_bytes,
            )
        )
        if len(hits) == args.limit:
            break
    note = None
    if not hits:
        note = (
            "Nothing matching is in the Recycle Bin (only files deleted from indexed folders "
            "are shown)."
        )
    return CheckRecycleBinOutput(results=hits, count=len(hits), supported=True, note=note)


# ---------- get_disk_usage ----------


class GetDiskUsageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    folder: str | None = Field(
        default=None, max_length=500, description="Report on this folder (optional)."
    )
    largest: int = Field(default=5, ge=0, le=20, description="How many largest files to list.")


class DriveUsage(BaseModel):
    drive: str
    total_gb: float
    used_gb: float
    free_gb: float
    used_percent: float


class FolderUsage(BaseModel):
    path: str
    files: int
    size_mb: float


class GetDiskUsageOutput(BaseModel):
    drives: list[DriveUsage]
    folders: list[FolderUsage]  # indexed folders (or the one asked for)
    largest_subfolders: list[FolderUsage]
    largest_files: list[FileHit]
    note: str | None = None


def _gb(n: int) -> float:
    return round(n / 1024**3, 1)


def _mb(n: int) -> float:
    return round(n / 1024**2, 2)


def _drives() -> list[DriveUsage]:
    drives: list[DriveUsage] = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = shutil.disk_usage(part.mountpoint)
        except OSError:
            continue  # empty card reader, disconnected network drive
        if usage.total == 0:
            continue
        drives.append(
            DriveUsage(
                drive=part.mountpoint,
                total_gb=_gb(usage.total),
                used_gb=_gb(usage.used),
                free_gb=_gb(usage.free),
                used_percent=round(100 * usage.used / usage.total, 1),
            )
        )
    return drives


def _get_disk_usage(deps: FileToolDeps, args: GetDiskUsageInput) -> GetDiskUsageOutput:
    folders = _folders(deps, [args.folder] if args.folder else None)
    usage: list[FolderUsage] = []
    children: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for folder in folders:
        rows = deps.index.sizes(folder)
        usage.append(
            FolderUsage(path=str(folder), files=len(rows), size_mb=_mb(sum(s for _, s in rows)))
        )
        for path, size in rows:
            parts = Path(path).relative_to(folder).parts
            if len(parts) > 1:  # files directly in the folder belong to no sub-folder
                bucket = children[str(folder / parts[0])]
                bucket[0] += 1
                bucket[1] += size
    subfolders = sorted(children.items(), key=lambda kv: kv[1][1], reverse=True)[:5]
    largest = deps.index.largest(args.largest * 3, folders=folders) if args.largest else []
    return GetDiskUsageOutput(
        drives=_drives(),
        folders=usage,
        largest_subfolders=[
            FolderUsage(path=p, files=c[0], size_mb=_mb(c[1])) for p, c in subfolders
        ],
        largest_files=_visible(deps, largest, args.largest) if args.largest else [],
        note=_index_note(deps)
        or "Folder sizes cover indexed folders only; drive totals cover the whole drive.",
    )


# ---------- registration ----------


def file_tool_specs(deps: FileToolDeps, status: ToolStatus = "quarantined") -> list[ToolSpec]:
    def manifest(
        name: str, description: str, tier: PermissionTier, **extra: object
    ) -> ToolManifest:
        return ToolManifest(
            name=name,
            version="0.1.0",
            created_by="developer",
            status=status,
            route="pc",
            description=description,
            permission_tier=tier,
            approval_required=False,
            side_effects="none",
            **extra,  # type: ignore[arg-type]
        )

    return [
        ToolSpec(
            manifest=manifest(
                "search_files",
                "Find files by words in their name or folder, in the folders the owner "
                "approved. Returns path, modified time and size. Does not read file contents.",
                PermissionTier.T0,
                permissions=_READ_SCOPE,
                timeout_s=10,
            ),
            input_model=SearchFilesInput,
            output_model=SearchFilesOutput,
            handler=lambda args: _search_files(deps, args),
        ),
        ToolSpec(
            manifest=manifest(
                "list_recent_files",
                "List the files changed in the last N days in the approved folders, newest "
                "first. Returns path, modified time and size.",
                PermissionTier.T0,
                permissions=_READ_SCOPE,
                timeout_s=10,
            ),
            input_model=ListRecentFilesInput,
            output_model=ListRecentFilesOutput,
            handler=lambda args: _list_recent_files(deps, args),
        ),
        ToolSpec(
            manifest=manifest(
                "check_recycle_bin",
                "Look in the Recycle Bin for deleted files by name. Only shows files that "
                "were deleted from approved folders. Read-only: it cannot restore anything.",
                PermissionTier.T1,
                permissions=ToolPermissions(
                    filesystem=["recycle_bin (records of files deleted from approved folders)"]
                ),
                timeout_s=15,
            ),
            input_model=CheckRecycleBinInput,
            output_model=CheckRecycleBinOutput,
            handler=lambda args: _check_recycle_bin(deps, args),
        ),
        ToolSpec(
            manifest=manifest(
                "get_disk_usage",
                "Report free and used space per drive, the size of the approved folders, "
                "their largest sub-folders and their largest files.",
                PermissionTier.T1,
                permissions=_READ_SCOPE,
                timeout_s=15,
            ),
            input_model=GetDiskUsageInput,
            output_model=GetDiskUsageOutput,
            handler=lambda args: _get_disk_usage(deps, args),
        ),
    ]


def default_file_tool_specs() -> list[ToolSpec]:
    """The file tools wired to the real index, `configs/permissions.yaml` and this OS."""
    deps = FileToolDeps(
        index=FileIndex(), policy=PathPolicy.from_config(), recycle_bin=get_recycle_bin()
    )
    return file_tool_specs(deps, status=FILE_TOOLS_STATUS)
