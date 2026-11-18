"""Step 8: the watcher keeps the index fresh and obeys the same path rules."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from memory.pc_index.watcher import IndexService
from tests.pc_layout import Layout, make_layout, write


@pytest.fixture
def pc(tmp_path: Path) -> Layout:
    return make_layout(tmp_path)


@pytest.fixture
def service(pc: Layout) -> Iterator[IndexService]:
    svc = IndexService(pc.index, pc.policy)
    svc.start()
    yield svc
    svc.stop()


def _wait(condition: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def _has(pc: Layout, word: str) -> bool:
    return bool(pc.index.search([word]))


def test_start_scans_first(pc: Layout, service: IndexService) -> None:
    assert service.roots == [pc.docs, pc.workspace]
    assert _has(pc, "annek")


def test_new_changed_moved_and_deleted_files_are_tracked(pc: Layout, service: IndexService) -> None:
    created = write(pc.docs / "projects" / "thesis-draft.txt", "v1")
    assert _wait(lambda: _has(pc, "thesis"))

    created.write_text("a much longer second version", encoding="utf-8")
    assert _wait(lambda: pc.index.search(["thesis"])[0].size > 2)

    created.rename(pc.docs / "projects" / "dissertation.txt")
    assert _wait(lambda: _has(pc, "dissertation") and not _has(pc, "thesis"))

    (pc.docs / "projects" / "dissertation.txt").unlink()
    assert _wait(lambda: not _has(pc, "dissertation"))


def test_a_file_dropped_into_a_protected_folder_is_never_indexed(
    pc: Layout, service: IndexService
) -> None:
    write(pc.secrets / "tokens-backup.txt")
    write(pc.docs / "projects" / "marker-file.txt")  # proves events are being handled
    assert _wait(lambda: _has(pc, "marker"))
    assert not _has(pc, "tokens")


def test_refresh_ignores_paths_outside_the_roots(pc: Layout) -> None:
    svc = IndexService(pc.index, pc.policy)
    svc.refresh(pc.outside / "annek-private.txt")
    svc.refresh(pc.secrets / "annek-passwords.txt")
    svc.refresh(pc.docs / "node_modules" / "pkg" / "index.js")
    assert pc.index.count() == 0
    svc.refresh(pc.docs / "projects")
    assert pc.index.count() == 2


def test_a_new_folder_is_indexed_as_a_folder(pc: Layout) -> None:
    svc = IndexService(pc.index, pc.policy)
    new = pc.docs / "family" / "amma"
    write(new / "photo.jpg")
    svc.refresh(pc.docs / "family")
    assert [e.name for e in pc.index.search(["amma"], kind="folder")] == ["amma"]
    # "family" itself first; "family/amma" also matches through its parent folder's name
    assert [e.name for e in pc.index.search(["family"], kind="folder")] == ["family", "amma"]
    svc.refresh(pc.secrets)  # protected: never indexed, not even its name
    assert pc.index.search(["secrets"], kind="folder") == []
