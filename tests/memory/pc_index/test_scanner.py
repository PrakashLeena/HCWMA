"""Step 8: the scanner indexes approved folders and the workspace, and nothing else."""

from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path

import pytest

from memory.pc_index.scanner import index_roots, scan
from security.permissions.path_check import PathPolicy
from tests.pc_layout import Layout, make_layout, write


@pytest.fixture
def pc(tmp_path: Path) -> Layout:
    return make_layout(tmp_path)


def _indexed(pc: Layout) -> set[str]:
    return {Path(p).name for p, _ in pc.index.sizes(pc.tmp)}


def _policy(pc: Layout, **overrides: object) -> PathPolicy:
    settings: dict[str, object] = {
        "repo_root": pc.repo,
        "home": pc.home,
        "workspace": "workspace",
        "approved_folders": [str(pc.docs)],
        "protected_paths": [],
        "blocked_folders": [],
    }
    settings.update(overrides)
    return PathPolicy(**settings)  # type: ignore[arg-type]


def test_roots_are_approved_folders_plus_workspace(pc: Layout) -> None:
    assert index_roots(pc.policy) == [pc.docs, pc.workspace]


def test_scan_indexes_approved_and_workspace_only(pc: Layout) -> None:
    report = scan(pc.index, pc.policy)
    assert _indexed(pc) == {
        "annek-proposal_v2.docx",
        "budget 2026.xlsx",
        "meeting notes.txt",
        "draft-report.md",
    }
    assert (report.files, report.folders) == (4, 2)  # folders: projects, notes
    assert report.changed == 6
    assert pc.index.get_meta("last_scan")


def test_protected_and_unapproved_files_never_reach_the_index(pc: Layout) -> None:
    scan(pc.index, pc.policy)
    names = _indexed(pc)
    assert "annek-passwords.txt" not in names  # protected path inside the approved folder
    assert "id_ed25519" not in names  # ~/.ssh
    assert "annek-private.txt" not in names  # not approved
    assert "index.js" not in names  # node_modules is skipped
    assert pc.index.sizes(pc.secrets) == []


@pytest.mark.parametrize("folder", [".venv", ".git", "node_modules", "__pycache__"])
def test_tool_cache_folders_are_never_indexed(pc: Layout, folder: str) -> None:
    write(pc.docs / "project" / folder / "deep" / "cached-file.bin")
    write(pc.docs / "project" / "main.py")
    scan(pc.index, pc.policy)
    names = _indexed(pc)
    assert "main.py" in names and "cached-file.bin" not in names


def test_protected_folder_is_skipped_even_when_its_parent_is_approved(pc: Layout) -> None:
    write(pc.home / "todo.txt")
    scan(pc.index, _policy(pc, approved_folders=[str(pc.home)], protected_paths=["~/.ssh"]))
    assert "todo.txt" in _indexed(pc) and "id_ed25519" not in _indexed(pc)


def test_blocked_folder_is_skipped(pc: Layout) -> None:
    scan(pc.index, _policy(pc, blocked_folders=[str(pc.docs / "notes")]))
    assert "meeting notes.txt" not in _indexed(pc)


def test_rescan_picks_up_changes_and_deletions(pc: Layout) -> None:
    scan(pc.index, pc.policy)
    (pc.docs / "projects" / "budget 2026.xlsx").unlink()
    write(pc.docs / "projects" / "new-plan.md")
    report = scan(pc.index, pc.policy)
    # new-plan.md, plus the 'projects' folder whose modified time moved
    assert report.removed == 1 and report.changed >= 1
    assert "new-plan.md" in _indexed(pc) and "budget 2026.xlsx" not in _indexed(pc)


def test_unapproving_a_folder_empties_it_from_the_index(pc: Layout) -> None:
    scan(pc.index, pc.policy)
    scan(pc.index, _policy(pc, approved_folders=[]))
    assert _indexed(pc) == {"draft-report.md"}


def test_no_approved_folders_and_no_workspace_is_an_empty_index(pc: Layout) -> None:
    report = scan(pc.index, _policy(pc, repo_root=pc.tmp / "empty", approved_folders=[]))
    assert report.roots == []
    assert pc.index.count() == 0


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlink support")
def test_links_are_not_followed(pc: Layout) -> None:
    try:
        os.symlink(pc.outside, pc.docs / "link-to-outside", target_is_directory=True)
        os.symlink(pc.home / ".ssh" / "id_ed25519", pc.docs / "key-link.txt")
    except OSError:
        pytest.skip("creating symlinks needs Developer Mode or admin on Windows")
    scan(pc.index, pc.policy)
    assert "annek-private.txt" not in _indexed(pc)
    assert "key-link.txt" not in _indexed(pc)


def _folders(pc: Layout) -> set[str]:
    """Names of every folder row in the index, read straight from the database."""
    import sqlite3

    with closing(sqlite3.connect(pc.index.db_path)) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM files WHERE kind = 'folder'")}


def test_sub_folders_are_indexed_as_folders(pc: Layout) -> None:
    write(pc.docs / "family" / "amma" / "photo.jpg")
    scan(pc.index, pc.policy)
    hits = pc.index.search(["amma"], kind="folder")
    assert [(Path(h.path).name, h.kind, h.size) for h in hits] == [("amma", "folder", 0)]
    assert pc.index.count("folder") == 4  # projects, notes, family, family/amma
    assert pc.index.count() == 5  # files only, as before


def test_protected_skipped_and_unapproved_folders_are_never_indexed(pc: Layout) -> None:
    write(pc.docs / "project" / ".venv" / "lib" / "x.py")
    write(pc.docs / "project" / ".git" / "objects" / "y")
    write(pc.docs / "project" / "__pycache__" / "z.pyc")
    scan(pc.index, pc.policy)
    folders = _folders(pc)
    # not secrets (protected), node_modules/.venv/.git/__pycache__ or anything under them
    assert folders == {"projects", "notes", "project"}


def test_index_from_before_folders_gains_the_kind_column(tmp_path: Path) -> None:
    import sqlite3

    from memory.pc_index.index import FileIndex

    db = tmp_path / "old.sqlite"
    with sqlite3.connect(db) as conn:  # the step-8 schema, without `kind`
        conn.executescript(
            "CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, "
            "dir TEXT NOT NULL, name TEXT NOT NULL, ext TEXT NOT NULL, size INTEGER NOT NULL, "
            "mtime REAL NOT NULL, root TEXT NOT NULL);"
            "INSERT INTO files VALUES (1, 'C:/r/a.txt', 'C:/r', 'a.txt', 'txt', 1, 0, 'C:/r');"
        )
    conn.close()
    index = FileIndex(db)
    assert index.count() == 1 and index.count("folder") == 0


def test_scan_reads_metadata_only_and_never_opens_a_file(
    pc: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Names, dates and sizes come from the directory listing and stat. No file is opened
    for reading, so a OneDrive cloud-only file is never downloaded (hydrated)."""
    import builtins
    import io

    opened: list[str] = []

    def refuse(*args: object, **kwargs: object) -> None:
        opened.append(str(args[0]) if args else "?")
        raise AssertionError(f"scan opened {args[0] if args else '?'}")

    for target, name in [
        (builtins, "open"),
        (io, "open"),
        (os, "open"),
        (Path, "open"),
        (Path, "read_bytes"),
        (Path, "read_text"),
    ]:
        monkeypatch.setattr(target, name, refuse)
    report = scan(pc.index, pc.policy)
    monkeypatch.undo()
    assert opened == []
    assert report.files == 4 and report.folders == 2


def test_the_index_never_lists_its_own_data_folder(
    pc: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 15: the repository sits inside an approved folder, so the index used to list its
    own database files (and re-index on every write to them)."""
    from memory.pc_index import scanner

    own = pc.docs / "hcwma" / "memory" / "pc_index" / "data"
    write(own / "index.sqlite", "db")
    write(own / "index.sqlite-wal", "wal")
    write(pc.docs / "hcwma" / "readme.md", "keep me")
    monkeypatch.setattr(scanner, "SKIP_DIRS", (own,))

    scan(pc.index, pc.policy)
    names = _indexed(pc)
    assert "readme.md" in names
    assert not names & {"index.sqlite", "index.sqlite-wal", "data"}
    assert scanner.has_skipped_part(own / "index.sqlite", pc.docs)
    assert scanner.entry_for(own / "index.sqlite", pc.docs, pc.policy) is None


def test_the_real_skip_list_is_the_index_folder() -> None:
    from memory.pc_index.index import DEFAULT_INDEX_PATH
    from memory.pc_index.scanner import is_own_data

    assert is_own_data(DEFAULT_INDEX_PATH)
    assert is_own_data(DEFAULT_INDEX_PATH.parent / "demo_pc" / "x.txt")
    assert not is_own_data(DEFAULT_INDEX_PATH.parents[1] / "scanner.py")
