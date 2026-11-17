"""SQLite store for long-term memory (master document 8, `docs/memory-policy.md` 4, 7, 8).

One table of entries plus an FTS5 full-text index over the entry text. Deleting is real:
the row and its index row are removed, freed pages are overwritten (`secure_delete`), the
write-ahead log is folded back in and the file is compacted, so a forgotten entry is not
left behind in the database file.

Each call opens its own short-lived connection, so the backend process and the tool server
process can both use the same file (WAL mode, busy timeout).
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from memory.guard import content_words
from memory.scopes import STORED_SCOPES

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entries (
    n            INTEGER PRIMARY KEY,
    id           TEXT NOT NULL UNIQUE,
    scope        TEXT NOT NULL,
    text         TEXT NOT NULL,
    source       TEXT NOT NULL,
    trust        TEXT NOT NULL,
    agent        TEXT,
    task_id      TEXT,
    session_id   TEXT,
    created_at   TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    expires_at   TEXT
);
CREATE INDEX IF NOT EXISTS entries_scope ON entries (scope, last_used_at);
CREATE INDEX IF NOT EXISTS entries_expiry ON entries (expires_at);
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
    text, tokenize = "unicode61 remove_diacritics 2 categories 'L* N* Co Mn Mc'"
);
"""
_COLUMNS = (
    "id, scope, text, source, trust, agent, task_id, session_id, "
    "created_at, last_used_at, expires_at"
)


class NotAMemoryDatabase(ValueError):
    """The file is not an HCWMA memory database (used before a restore)."""


class MemoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    scope: str
    text: str
    source: str  # owner_said | tool_result | summary
    trust: str  # high | low
    agent: str | None = None  # the agent a skill note belongs to
    task_id: str | None = None  # the request that produced it
    session_id: str | None = None  # the chat session that produced it
    created_at: str  # ISO 8601, UTC
    last_used_at: str
    expires_at: str | None = None


def new_entry_id() -> str:
    return uuid.uuid4().hex[:12]


def _fts_query(text: str, limit: int = 12) -> str:
    """An OR query of the meaningful words in `text`, each as a prefix match."""
    seen: list[str] = []
    for word in content_words(text):
        if word not in seen:
            seen.append(word)
    return " OR ".join(f'"{w}"*' for w in seen[:limit])


class MemoryStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(_SCHEMA)
            con.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=10)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA busy_timeout = 10000")
            con.execute("PRAGMA secure_delete = ON")
            yield con
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _entry(row: sqlite3.Row) -> MemoryEntry:
        return MemoryEntry(**{k: row[k] for k in row.keys() if k != "n"})

    # ---------- write ----------

    def add(self, entry: MemoryEntry) -> None:
        if entry.scope not in STORED_SCOPES:
            raise ValueError(f"'{entry.scope}' is not a stored memory scope")
        with self._lock, self._connect() as con:
            cur = con.execute(
                f"INSERT INTO entries ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",  # noqa: S608
                (
                    entry.id,
                    entry.scope,
                    entry.text,
                    entry.source,
                    entry.trust,
                    entry.agent,
                    entry.task_id,
                    entry.session_id,
                    entry.created_at,
                    entry.last_used_at,
                    entry.expires_at,
                ),
            )
            con.execute(
                "INSERT INTO entries_fts (rowid, text) VALUES (?, ?)", (cur.lastrowid, entry.text)
            )

    def replace(self, entry: MemoryEntry) -> None:
        """Rewrite an existing entry (same id) and its index row."""
        with self._lock, self._connect() as con:
            row = con.execute("SELECT n FROM entries WHERE id = ?", (entry.id,)).fetchone()
            if row is None:
                raise KeyError(entry.id)
            con.execute(
                "UPDATE entries SET text=?, source=?, trust=?, agent=?, task_id=?, session_id=?, "
                "last_used_at=?, expires_at=? WHERE id=?",
                (
                    entry.text,
                    entry.source,
                    entry.trust,
                    entry.agent,
                    entry.task_id,
                    entry.session_id,
                    entry.last_used_at,
                    entry.expires_at,
                    entry.id,
                ),
            )
            con.execute("DELETE FROM entries_fts WHERE rowid = ?", (row["n"],))
            con.execute(
                "INSERT INTO entries_fts (rowid, text) VALUES (?, ?)", (row["n"], entry.text)
            )

    def touch(self, ids: Iterable[str], when: str) -> None:
        ids = list(ids)
        if not ids:
            return
        with self._lock, self._connect() as con:
            con.executemany(
                "UPDATE entries SET last_used_at = ? WHERE id = ?", [(when, i) for i in ids]
            )

    # ---------- read ----------

    def get(self, entry_id: str) -> MemoryEntry | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        return self._entry(row) if row else None

    def list(
        self,
        scopes: Iterable[str] | None = None,
        *,
        agent: str | None = None,
        limit: int | None = None,
    ) -> list[MemoryEntry]:
        """Entries, most recently used first. `agent`: skill notes of that agent only."""
        where, params = self._filter(scopes, agent)
        sql = f"SELECT * FROM entries {where} ORDER BY last_used_at DESC, n DESC"  # noqa: S608
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._connect() as con:
            return [self._entry(r) for r in con.execute(sql, params)]

    def search(
        self,
        query: str,
        scopes: Iterable[str] | None = None,
        *,
        agent: str | None = None,
        limit: int = 5,
    ) -> list[MemoryEntry]:
        """Full-text search, best match first."""
        match = _fts_query(query)
        if not match:
            return []
        where, params = self._filter(scopes, agent, prefix="AND")
        sql = (
            "SELECT e.* FROM entries_fts f JOIN entries e ON e.n = f.rowid "  # noqa: S608
            f"WHERE entries_fts MATCH ? {where} "
            "ORDER BY bm25(entries_fts), e.last_used_at DESC LIMIT ?"
        )
        with self._connect() as con:
            return [self._entry(r) for r in con.execute(sql, [match, *params, limit])]

    def in_full_text_index(self, word: str) -> bool:
        """True if any index row still matches `word` (used to prove a delete)."""
        match = _fts_query(word)
        if not match:
            return False
        with self._connect() as con:
            row = con.execute(
                "SELECT count(*) FROM entries_fts WHERE entries_fts MATCH ?", (match,)
            ).fetchone()
        return bool(row[0])

    def expired(self, now: str) -> list[MemoryEntry]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM entries WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)
            )
            return [self._entry(r) for r in rows]

    def count(self) -> int:
        with self._connect() as con:
            return int(con.execute("SELECT count(*) FROM entries").fetchone()[0])

    @staticmethod
    def _filter(
        scopes: Iterable[str] | None, agent: str | None, prefix: str = "WHERE"
    ) -> tuple[str, list[object]]:
        clauses: list[str] = []
        params: list[object] = []
        if scopes is not None:
            wanted = [s for s in scopes if s in STORED_SCOPES]
            if not wanted:
                return f"{prefix} 0", []
            clauses.append(f"scope IN ({','.join('?' * len(wanted))})")
            params.extend(wanted)
        if agent is not None:
            clauses.append("(agent IS NULL OR agent = ?)")
            params.append(agent)
        return (f"{prefix} " + " AND ".join(clauses)) if clauses else "", params

    # ---------- delete (real removal) ----------

    def delete(self, ids: Iterable[str]) -> int:
        ids = list(dict.fromkeys(ids))
        if not ids:
            return 0
        removed = 0
        with self._lock:
            with self._connect() as con:
                for entry_id in ids:
                    row = con.execute("SELECT n FROM entries WHERE id = ?", (entry_id,)).fetchone()
                    if row is None:
                        continue
                    con.execute("DELETE FROM entries_fts WHERE rowid = ?", (row["n"],))
                    con.execute("DELETE FROM entries WHERE n = ?", (row["n"],))
                    removed += 1
            if removed:
                self._scrub()
        return removed

    def delete_all(self) -> int:
        with self._lock:
            with self._connect() as con:
                removed = int(con.execute("SELECT count(*) FROM entries").fetchone()[0])
                con.execute("DELETE FROM entries_fts")
                con.execute("DELETE FROM entries")
            self._scrub()
        return removed

    def _scrub(self) -> None:
        """Leave no copy of deleted text in the index segments, the WAL or free pages."""
        con = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            con.execute("PRAGMA busy_timeout = 10000")
            con.execute("PRAGMA secure_delete = ON")
            con.execute("INSERT INTO entries_fts (entries_fts) VALUES ('optimize')")
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.execute("VACUUM")
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            con.close()

    # ---------- backup / restore ----------

    def backup_to(self, dest: Path) -> None:
        """A consistent copy of the whole database in one file."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as src:
            out = sqlite3.connect(dest)
            try:
                src.backup(out)
                out.execute("PRAGMA journal_mode = DELETE")
                out.commit()
            finally:
                out.close()

    def restore_from(self, src_path: Path) -> None:
        """Replace everything in this database with the contents of a backup file."""
        check_is_memory_database(src_path)
        with self._lock:
            src = sqlite3.connect(f"{src_path.resolve().as_uri()}?mode=ro", uri=True)
            dst = sqlite3.connect(self.path, timeout=10)
            try:
                src.backup(dst)
                dst.commit()
            finally:
                src.close()
                dst.close()
            self._scrub()


def check_is_memory_database(path: Path) -> int:
    """Number of entries in `path`; raises `NotAMemoryDatabase` if it is something else."""
    if not path.is_file():
        raise NotAMemoryDatabase(f"there is no backup file at '{path}'")
    try:
        con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            version = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            count = con.execute("SELECT count(*) FROM entries").fetchone()[0]
            con.execute("SELECT count(*) FROM entries_fts").fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise NotAMemoryDatabase(f"'{path}' is not an HCWMA memory database") from exc
    if version is None or int(version[0]) > SCHEMA_VERSION:
        raise NotAMemoryDatabase(f"'{path}' was written by a newer HCWMA version")
    return int(count)
