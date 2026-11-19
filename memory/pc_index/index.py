"""PC file index: SQLite with full-text search over file names and folders (build step 8).

Holds metadata only — path, name, extension, size, modified time, and whether the row is a
file or a folder (folders are indexed too, so "find the folder amma" works). No file
contents are stored (owner decision 2026-11-17). The scanner and the watcher write; the file
tools read.

Every operation opens its own short-lived connection, so the index is safe to use from tool
worker threads and from two processes at once (the backend writes, the MCP server reads; WAL
mode). The database lives in `memory/pc_index/data/`, which is gitignored and is not inside
any approved folder, so no agent tool can read it directly.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DEFAULT_INDEX_PATH = Path(__file__).resolve().parent / "data" / "index.sqlite"
# Tests point this at a temp file so they never read or change the real PC index.
INDEX_PATH_ENV = "HCWMA_INDEX_PATH"


def default_index_path() -> Path:
    override = os.environ.get(INDEX_PATH_ENV)
    return Path(override) if override else DEFAULT_INDEX_PATH


_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id    INTEGER PRIMARY KEY,
    path  TEXT NOT NULL UNIQUE COLLATE NOCASE,
    dir   TEXT NOT NULL,
    name  TEXT NOT NULL,
    ext   TEXT NOT NULL,
    size  INTEGER NOT NULL,
    mtime REAL NOT NULL,
    root  TEXT NOT NULL COLLATE NOCASE,
    kind  TEXT NOT NULL DEFAULT 'file'
);
CREATE INDEX IF NOT EXISTS files_mtime ON files (mtime);
CREATE INDEX IF NOT EXISTS files_root ON files (root);
CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(name, dir, tokenize = 'trigram');
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_MIN_FTS_CHARS = 3  # the trigram tokenizer cannot match shorter terms
# Rows read from SQLite before ranking. The trigram index finds "amma" inside "Grammarly"
# as readily as the folder "amma"; `match_score` then puts the better match first.
_CANDIDATES = 500

Kind = Literal["file", "folder"]


@dataclass(frozen=True)
class FileEntry:
    """One indexed file or folder. `path` and `root` are real, absolute paths."""

    path: str
    size: int
    mtime: float
    root: str
    kind: Kind = "file"

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def dir(self) -> str:
        return os.path.dirname(self.path)

    @property
    def ext(self) -> str:
        if self.kind == "folder":
            return ""
        return os.path.splitext(self.path)[1].lstrip(".").lower()

    @property
    def search_dir(self) -> str:
        """The folder as searched: from the root's own name down, not the whole drive path,
        so a word in the path above the root (a user name, say) does not match every file."""
        try:
            return os.path.relpath(self.dir, os.path.dirname(self.root))
        except ValueError:
            return self.dir


def query_terms(query: str) -> list[str]:
    """Lower-case words of a search query, split the way file names are split."""
    seen: dict[str, None] = {}
    for word in _WORD.findall(query.lower()):
        seen.setdefault(word)
    return list(seen)


def _words(text: str) -> list[str]:
    """Lower-case words of a name: split on spaces, dots, dashes, underscores and camelCase."""
    return _WORD.findall(_CAMEL.sub(" ", text).lower())


def match_score(entry: FileEntry, terms: Sequence[str]) -> float:
    """How well a search hit matches; higher is better. Per term:

    4    the whole name (without extension) is the term ("amma" -> folder "amma")
    3    a whole word of the name ("annek" -> "annek-proposal_v2.docx")
    2    the name or one of its words starts with the term ("amma" -> "ammaPhotos")
    1.5  a whole word of a folder above it ("amma" -> "amma/photo.jpg")
    1    anywhere else in the name ("amma" -> "GrammarlyInstaller.exe")
    """
    stem = entry.name if entry.kind == "folder" else os.path.splitext(entry.name)[0]
    stem_low = stem.lower()
    words = _words(stem)
    dir_words = set(_words(entry.search_dir))
    score = 0.0
    for term in terms:
        if stem_low == term:
            score += 4
        elif term in words:
            score += 3
        elif stem_low.startswith(term) or any(w.startswith(term) for w in words):
            score += 2
        elif term in dir_words:
            score += 1.5
        elif term in entry.name.lower():
            score += 1
    return score


def _prefix(folder: str | Path) -> str:
    text = str(folder)
    return text if text.endswith(os.sep) else text + os.sep


def _like(term: str) -> str:
    escaped = term.replace("^", "^^").replace("%", "^%").replace("_", "^_")
    return f"%{escaped}%"


class FileIndex:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path if db_path is not None else default_index_path()
        self._ready = False
        self._lock = threading.Lock()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if not self._ready:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
                    conn.execute("PRAGMA journal_mode = WAL")
                    conn.executescript(_SCHEMA)
                    columns = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
                    if "kind" not in columns:  # built before folders were indexed
                        conn.execute(
                            "ALTER TABLE files ADD COLUMN kind TEXT NOT NULL DEFAULT 'file'"
                        )
                self._ready = True
        with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
            conn.row_factory = sqlite3.Row
            with conn:  # one transaction per operation
                yield conn

    # ---- writing (scanner and watcher) ----

    @staticmethod
    def _delete_ids(conn: sqlite3.Connection, ids: Sequence[int]) -> None:
        for rowid in ids:
            conn.execute("DELETE FROM files_fts WHERE rowid = ?", (rowid,))
            conn.execute("DELETE FROM files WHERE id = ?", (rowid,))

    @staticmethod
    def _upsert(conn: sqlite3.Connection, entry: FileEntry) -> None:
        old = conn.execute("SELECT id FROM files WHERE path = ?", (entry.path,)).fetchone()
        if old is not None:
            FileIndex._delete_ids(conn, [old["id"]])
        cur = conn.execute(
            "INSERT INTO files (path, dir, name, ext, size, mtime, root, kind) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                entry.path,
                entry.dir,
                entry.name,
                entry.ext,
                entry.size,
                entry.mtime,
                entry.root,
                entry.kind,
            ),
        )
        conn.execute(
            "INSERT INTO files_fts (rowid, name, dir) VALUES (?,?,?)",
            (cur.lastrowid, entry.name, entry.search_dir),
        )

    def upsert(self, entries: Iterable[FileEntry]) -> int:
        count = 0
        with self._connect() as conn:
            for entry in entries:
                self._upsert(conn, entry)
                count += 1
        return count

    def remove(self, path: str | Path) -> int:
        """Remove one file, or everything under it if it was a folder."""
        text = str(path)
        prefix = _prefix(text)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM files WHERE path = ? OR substr(path, 1, ?) = ? COLLATE NOCASE",
                (text, len(prefix), prefix),
            ).fetchall()
            self._delete_ids(conn, [r["id"] for r in rows])
        return len(rows)

    def sync_tree(self, folder: str | Path, entries: Iterable[FileEntry]) -> tuple[int, int]:
        """Make the index match `entries` for everything under `folder`.

        Returns (added or changed, removed). Unchanged files are not rewritten.
        """
        prefix = _prefix(folder)
        changed = 0
        with self._connect() as conn:
            known = {
                os.path.normcase(r["path"]): (r["id"], r["size"], r["mtime"], r["kind"])
                for r in conn.execute(
                    "SELECT id, path, size, mtime, kind FROM files "
                    "WHERE substr(path, 1, ?) = ? COLLATE NOCASE",
                    (len(prefix), prefix),
                )
            }
            for entry in entries:
                old = known.pop(os.path.normcase(entry.path), None)
                if old is not None and old[1:] == (entry.size, entry.mtime, entry.kind):
                    continue
                self._upsert(conn, entry)
                changed += 1
            self._delete_ids(conn, [v[0] for v in known.values()])
        return changed, len(known)

    def keep_only_roots(self, roots: Sequence[str | Path]) -> int:
        """Drop files whose root is no longer indexed (a folder the owner un-approved)."""
        keep = {os.path.normcase(str(r)) for r in roots}
        with self._connect() as conn:
            rows = conn.execute("SELECT id, root FROM files").fetchall()
            stale = [r["id"] for r in rows if os.path.normcase(r["root"]) not in keep]
            self._delete_ids(conn, stale)
        return len(stale)

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # ---- reading (file tools) ----

    def get_meta(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row["value"]

    def count(self, kind: Kind = "file") -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT count(*) FROM files WHERE kind = ?", (kind,)).fetchone()
            return int(row[0])

    @staticmethod
    def _scope(
        folders: Sequence[str | Path] | None, ext: str | None, kind: Kind | None = "file"
    ) -> tuple[str, list[object]]:
        clauses: list[str] = []
        params: list[object] = []
        if kind is not None:
            clauses.append("f.kind = ?")
            params.append(kind)
        if folders is not None:
            parts = []
            for folder in folders:
                prefix = _prefix(folder)
                parts.append("substr(f.path, 1, ?) = ? COLLATE NOCASE")
                params += [len(prefix), prefix]
            clauses.append("(" + " OR ".join(parts) + ")" if parts else "0")
        if ext:
            clauses.append("f.ext = ?")
            params.append(ext.lstrip(".").lower())
        return "".join(f" AND {c}" for c in clauses), params

    @staticmethod
    def _entries(rows: Iterable[sqlite3.Row]) -> list[FileEntry]:
        return [
            FileEntry(
                path=r["path"], size=r["size"], mtime=r["mtime"], root=r["root"], kind=r["kind"]
            )
            for r in rows
        ]

    def search(
        self,
        terms: Sequence[str],
        *,
        folders: Sequence[str | Path] | None = None,
        ext: str | None = None,
        limit: int = 50,
        any_term: bool = False,
        kind: Kind | None = None,
    ) -> list[FileEntry]:
        """Files and folders whose name or folder contains every term (or any term).

        Best match first (`match_score`): a whole-word or name-start match ranks above the
        same letters inside a longer name. `kind` limits the hits to files or to folders.
        """
        scope, scope_params = self._scope(folders, ext, kind)
        long = [t for t in terms if len(t) >= _MIN_FTS_CHARS]
        short = [t for t in terms if len(t) < _MIN_FTS_CHARS]
        if not long and not short and not ext:
            return []
        joiner = " OR " if any_term else " AND "
        with self._connect() as conn:
            if long:
                match = joiner.join('"' + t.replace('"', '""') + '"' for t in long)
                sql = (
                    "SELECT f.* FROM files_fts JOIN files f ON f.id = files_fts.rowid "
                    "WHERE files_fts MATCH ?"
                )
                params: list[object] = [match]
                if short and not any_term:
                    sql += "".join(" AND f.name LIKE ? ESCAPE '^'" for _ in short)
                    params += [_like(t) for t in short]
                sql += scope + " ORDER BY bm25(files_fts, 10.0, 1.0), f.mtime DESC LIMIT ?"
            else:
                sql = "SELECT f.* FROM files f WHERE 1"
                params = []
                if short:
                    likes = joiner.join("f.name LIKE ? ESCAPE '^'" for _ in short)
                    sql += f" AND ({likes})"
                    params += [_like(t) for t in short]
                sql += scope + " ORDER BY f.mtime DESC LIMIT ?"
            fetched = self._entries(
                conn.execute(sql, [*params, *scope_params, max(limit, _CANDIDATES)])
            )
        # sorted() is stable: equal scores keep SQLite's order (bm25, then newest first).
        return sorted(fetched, key=lambda e: -match_score(e, terms))[:limit]

    def recent(
        self,
        since: float,
        *,
        folders: Sequence[str | Path] | None = None,
        ext: str | None = None,
        limit: int = 50,
    ) -> list[FileEntry]:
        scope, params = self._scope(folders, ext)
        sql = f"SELECT f.* FROM files f WHERE f.mtime >= ?{scope} ORDER BY f.mtime DESC LIMIT ?"  # noqa: S608 - scope is built from fixed clauses, values are bound
        with self._connect() as conn:
            return self._entries(conn.execute(sql, [since, *params, limit]))

    def largest(
        self, limit: int, *, folders: Sequence[str | Path] | None = None
    ) -> list[FileEntry]:
        scope, params = self._scope(folders, None)
        sql = f"SELECT f.* FROM files f WHERE 1{scope} ORDER BY f.size DESC LIMIT ?"  # noqa: S608 - as above
        with self._connect() as conn:
            return self._entries(conn.execute(sql, [*params, limit]))

    def sizes(self, folder: str | Path) -> list[tuple[str, int]]:
        """(path, size) of every indexed file (not folder) under `folder`."""
        prefix = _prefix(folder)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT path, size FROM files "
                "WHERE kind = 'file' AND substr(path, 1, ?) = ? COLLATE NOCASE",
                (len(prefix), prefix),
            ).fetchall()
        return [(r["path"], r["size"]) for r in rows]
