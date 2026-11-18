"""Step 8: the SQLite full-text file index."""

from __future__ import annotations

from pathlib import Path

from memory.pc_index.index import FileEntry, FileIndex, query_terms


def _entry(root: Path, rel: str, size: int = 1, mtime: float = 100.0) -> FileEntry:
    return FileEntry(path=str(root / rel), size=size, mtime=mtime, root=str(root))


def _names(entries: list[FileEntry]) -> list[str]:
    return [e.name for e in entries]


def _filled(tmp_path: Path) -> tuple[FileIndex, Path]:
    root = tmp_path / "docs"
    index = FileIndex(tmp_path / "db" / "index.sqlite")
    index.upsert(
        [
            _entry(root, "projects/annek-proposal_v2.docx", size=30, mtime=300),
            _entry(root, "projects/budget 2026.xlsx", size=500, mtime=200),
            _entry(root, "notes/Meeting Notes.TXT", size=5, mtime=100),
            _entry(root, "notes/a1.txt", size=1, mtime=50),
        ]
    )
    return index, root


def test_query_terms_split_like_file_names() -> None:
    assert query_terms("Annek-Proposal_v2.docx  annek") == ["annek", "proposal", "v2", "docx"]
    assert query_terms("  ") == []


def test_search_matches_part_of_a_name_in_any_case(tmp_path: Path) -> None:
    index, _ = _filled(tmp_path)
    assert _names(index.search(["annek"])) == ["annek-proposal_v2.docx"]
    assert _names(index.search(["propos"])) == ["annek-proposal_v2.docx"]
    assert _names(index.search(["meeting"])) == ["Meeting Notes.TXT"]


def test_all_terms_must_match_unless_any_term(tmp_path: Path) -> None:
    index, _ = _filled(tmp_path)
    assert index.search(["annek", "budget"]) == []
    assert set(_names(index.search(["annek", "budget"], any_term=True))) == {
        "annek-proposal_v2.docx",
        "budget 2026.xlsx",
    }


def test_short_terms_and_extension_filter(tmp_path: Path) -> None:
    index, _ = _filled(tmp_path)
    assert _names(index.search(["annek", "v2"])) == ["annek-proposal_v2.docx"]
    assert _names(index.search(["a1"])) == ["a1.txt"]
    assert set(_names(index.search([], ext=".txt"))) == {"Meeting Notes.TXT", "a1.txt"}
    assert index.search([]) == []


def test_like_wildcards_in_a_query_are_literal(tmp_path: Path) -> None:
    index, _ = _filled(tmp_path)
    assert index.search(["%"]) == []
    assert _names(index.search(["_"])) == ["annek-proposal_v2.docx"]


def test_search_can_be_limited_to_folders(tmp_path: Path) -> None:
    index, root = _filled(tmp_path)
    # a folder name matches too, but a match in the file name ranks first
    assert _names(index.search(["notes"])) == ["Meeting Notes.TXT", "a1.txt"]
    assert _names(index.search(["budget"], folders=[root / "projects"])) == ["budget 2026.xlsx"]
    assert index.search(["annek"], folders=[root / "notes"]) == []
    assert index.search(["annek"], folders=[]) == []
    # a sibling folder whose name merely starts with "docs" is not inside "docs"
    assert index.search(["annek"], folders=[str(root) + "2"]) == []


def test_words_in_the_path_above_the_root_do_not_match(tmp_path: Path) -> None:
    index, root = _filled(tmp_path)
    assert index.search([root.parent.name.lower()[:12]]) == []
    assert len(index.search(["docs"])) == 4  # the root's own name does


def test_recent_largest_and_sizes(tmp_path: Path) -> None:
    index, root = _filled(tmp_path)
    assert _names(index.recent(150)) == ["annek-proposal_v2.docx", "budget 2026.xlsx"]
    assert _names(index.largest(1)) == ["budget 2026.xlsx"]
    assert sum(size for _, size in index.sizes(root / "projects")) == 530


def test_upsert_replaces_and_remove_handles_files_and_folders(tmp_path: Path) -> None:
    index, root = _filled(tmp_path)
    index.upsert([_entry(root, "projects/annek-proposal_v2.docx", size=99, mtime=999)])
    assert index.count() == 4
    assert index.search(["annek"])[0].size == 99
    assert index.remove(root / "notes" / "a1.txt") == 1
    assert index.remove(root / "projects") == 2
    assert index.search(["annek"]) == []
    assert index.count() == 1


def test_sync_tree_adds_updates_and_drops(tmp_path: Path) -> None:
    index, root = _filled(tmp_path)
    changed, removed = index.sync_tree(
        root / "notes",
        [_entry(root, "notes/a1.txt", size=1, mtime=50), _entry(root, "notes/new.txt")],
    )
    assert (changed, removed) == (1, 1)  # a1 unchanged, new added, Meeting Notes dropped
    assert index.count() == 4
    assert index.search(["meeting"]) == []


def test_keep_only_roots_and_meta(tmp_path: Path) -> None:
    index, root = _filled(tmp_path)
    other = tmp_path / "old"
    index.upsert([_entry(other, "gone.txt")])
    assert index.keep_only_roots([root]) == 1
    assert index.count() == 4
    assert index.get_meta("last_scan") is None
    index.set_meta("last_scan", "a")
    index.set_meta("last_scan", "b")
    assert index.get_meta("last_scan") == "b"


# ---- folders and ranking (real-use bug 2026-11-19: "find folder amma") ----


def _folder(root: Path, rel: str, mtime: float = 100.0) -> FileEntry:
    return FileEntry(path=str(root / rel), size=0, mtime=mtime, root=str(root), kind="folder")


def _amma(tmp_path: Path) -> tuple[FileIndex, Path]:
    root = tmp_path / "home"
    index = FileIndex(tmp_path / "db" / "index.sqlite")
    index.upsert(
        [
            # newest first in SQLite's own order, so ranking has to do the work
            _entry(root, "Downloads/GrammarlyInstaller.exe", mtime=900),
            _entry(root, "Downloads/programma.pdf", mtime=800),
            _entry(root, "Desktop/ammaPhotos.zip", mtime=300),
            _entry(root, "Desktop/amma/birthday.jpg", mtime=200),
            _folder(root, "Desktop/amma", mtime=100),
        ]
    )
    return index, root


def test_whole_name_and_word_start_rank_above_substring_matches(tmp_path: Path) -> None:
    index, _ = _amma(tmp_path)
    names = _names(index.search(["amma"]))
    assert names[:3] == [
        "amma",  # the whole name
        "ammaPhotos.zip",  # name starts with it
        "birthday.jpg",  # inside a folder called amma
    ]
    # only letters inside a longer word: last, in SQLite's relevance order
    assert set(names[3:]) == {"GrammarlyInstaller.exe", "programma.pdf"}


def test_kind_limits_search_to_folders_or_files(tmp_path: Path) -> None:
    index, _ = _amma(tmp_path)
    folders = index.search(["amma"], kind="folder")
    assert [(e.name, e.kind, e.ext) for e in folders] == [("amma", "folder", "")]
    assert "amma" not in _names(index.search(["amma"], kind="file"))


def test_folders_stay_out_of_recent_largest_and_sizes(tmp_path: Path) -> None:
    index, root = _amma(tmp_path)
    assert "amma" not in _names(index.recent(0))
    assert "amma" not in _names(index.largest(10))
    assert all(not p.endswith("amma") for p, _ in index.sizes(root))
    assert (index.count(), index.count("folder")) == (4, 1)


def test_match_score_orders_the_kinds_of_match() -> None:
    from memory.pc_index.index import match_score

    root = Path("C:/r")

    def score(rel: str, *terms: str, kind: str = "file") -> float:
        e = FileEntry(path=str(root / rel), size=0, mtime=0, root=str(root), kind=kind)  # type: ignore[arg-type]
        return match_score(e, terms)

    assert score("amma", "amma", kind="folder") == 4
    assert score("amma.txt", "amma") == 4  # the extension is not part of the name
    assert score("annek-proposal_v2.docx", "annek") == 3
    assert score("AmmaPhotos.zip", "photos") == 3  # camelCase splits into words
    assert score("ammaphotos.zip", "amma") == 2
    assert score("amma/x.jpg", "amma") == 1.5
    assert score("GrammarlyInstaller.exe", "amma") == 1
    assert score("annek-proposal_v2.docx", "annek", "proposal") == 6
