"""Walk the indexed folders and bring the PC file index up to date (build step 8).

What gets indexed: the owner's approved folders plus the HCWMA workspace — exactly the
places `PathPolicy.check(path, "read")` allows. Everything else is skipped:

- protected paths and owner-blocked folders (rule 10) are pruned, never opened;
- symlinks and junctions are not followed, so a link cannot pull an outside folder in;
- tool caches such as `.git` and `node_modules` are skipped as noise.

Sub-folders are indexed as entries of their own (kind "folder"), so a folder can be found by
name. Only `os.scandir` metadata is read (name, size, modified time). No file is opened.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from memory.pc_index.index import FileEntry, FileIndex
from security.permissions.path_check import PathDenied, PathPolicy

SKIP_DIR_NAMES = frozenset(
    {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv"}
    | {".pytest_cache", ".ruff_cache", ".mypy_cache", "$recycle.bin", "system volume information"}
)
META_LAST_SCAN = "last_scan"


@dataclass(frozen=True)
class ScanReport:
    roots: list[str]
    files: int
    changed: int
    removed: int
    folders: int = 0


def _same(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def _inside(child: str | Path, parent: str | Path) -> bool:
    c, p = os.path.normcase(str(child)), os.path.normcase(str(parent))
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def index_roots(policy: PathPolicy) -> list[Path]:
    """Approved folders + the workspace that exist, without folders nested in another root."""
    candidates: list[Path] = []
    for folder in [*policy.approved, policy.workspace]:
        if folder.is_dir() and not any(_same(folder, c) for c in candidates):
            candidates.append(folder)
    return [
        c
        for c in candidates
        if not any(_inside(c, other) and not _same(c, other) for other in candidates)
    ]


def root_of(path: str | Path, roots: list[Path]) -> Path | None:
    return next((r for r in roots if _inside(path, r)), None)


def allowed_dir(path: Path, policy: PathPolicy) -> bool:
    """True if the scanner may list `path`: readable by policy and not a link elsewhere."""
    if path.name.lower() in SKIP_DIR_NAMES:
        return False
    try:
        real = policy.check(path, "read")
    except (PathDenied, OSError):
        return False
    return _same(real, path)


def entry_for(path: Path, root: Path, policy: PathPolicy) -> FileEntry | None:
    """The index entry for one file, or None if it must not be indexed or is gone."""
    try:
        if path.is_symlink() or not _same(policy.check(path, "read"), path):
            return None
        stat = path.stat()
    except (PathDenied, OSError):
        return None
    if not path.is_file():
        return None
    return FileEntry(path=str(path), size=stat.st_size, mtime=stat.st_mtime, root=str(root))


def folder_entry_for(path: Path, root: Path, policy: PathPolicy) -> FileEntry | None:
    """The index entry for one folder (not the root itself), or None if it must not be
    indexed: a tool cache, a protected or blocked folder, a link, or gone."""
    if _same(path, root) or not allowed_dir(path, policy):
        return None
    try:
        if path.is_symlink() or not path.is_dir():
            return None
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return FileEntry(path=str(path), size=0, mtime=mtime, root=str(root), kind="folder")


def has_skipped_part(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return True
    return any(part.lower() in SKIP_DIR_NAMES for part in parts[:-1])


def iter_files(folder: Path, root: Path, policy: PathPolicy) -> Iterator[FileEntry]:
    """Every indexable file and sub-folder under `folder` (which belongs to `root`)."""
    stack = [folder]
    while stack:
        current = stack.pop()
        if not _same(current, root) and not allowed_dir(current, policy):
            continue
        try:
            with os.scandir(current) as entries:
                children = list(entries)
        except OSError:
            continue
        for child in children:
            child_path = Path(child.path)
            try:
                if child.is_symlink():
                    continue
                if child.is_dir(follow_symlinks=False):
                    folder_entry = folder_entry_for(child_path, root, policy)
                    if folder_entry is not None:
                        stack.append(child_path)
                        yield folder_entry
                    continue
            except OSError:
                continue
            entry = entry_for(child_path, root, policy)
            if entry is not None:
                yield entry


def scan_tree(index: FileIndex, policy: PathPolicy, folder: Path, root: Path) -> tuple[int, int]:
    """Re-index one folder inside a root. Returns (files seen, changed + removed)."""
    seen = 0

    def counted() -> Iterator[FileEntry]:
        nonlocal seen
        for entry in iter_files(folder, root, policy):
            seen += 1
            yield entry

    changed, removed = index.sync_tree(folder, counted())
    return seen, changed + removed


def scan(index: FileIndex, policy: PathPolicy) -> ScanReport:
    """Full scan of every indexed root; also drops roots that are no longer approved."""
    roots = [r for r in index_roots(policy) if allowed_dir(r, policy)]
    seen = {"file": 0, "folder": 0}
    changed = 0
    removed = index.keep_only_roots(roots)
    for root in roots:

        def counted(root: Path = root) -> Iterator[FileEntry]:
            for entry in iter_files(root, root, policy):
                seen[entry.kind] += 1
                yield entry

        c, r = index.sync_tree(root, counted())
        changed += c
        removed += r
    index.set_meta(META_LAST_SCAN, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return ScanReport(
        roots=[str(r) for r in roots],
        files=seen["file"],
        changed=changed,
        removed=removed,
        folders=seen["folder"],
    )
