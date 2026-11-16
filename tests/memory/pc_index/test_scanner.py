"""Step 8: the scanner indexes approved folders and the workspace, and nothing else."""

from __future__ import annotations

import os
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
    assert report.files == 4 and report.changed == 4
    assert pc.index.get_meta("last_scan")


def test_protected_and_unapproved_files_never_reach_the_index(pc: Layout) -> None:
    scan(pc.index, pc.policy)
    names = _indexed(pc)
    assert "annek-passwords.txt" not in names  # protected path inside the approved folder
    assert "id_ed25519" not in names  # ~/.ssh
    assert "annek-private.txt" not in names  # not approved
    assert "index.js" not in names  # node_modules is skipped
    assert pc.index.sizes(pc.secrets) == []


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
    assert (report.changed, report.removed) == (1, 1)
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
