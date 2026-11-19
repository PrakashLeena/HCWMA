"""The Planner's own database: reminders, to-dos and deadlines (build step 15).

One SQLite file in the HCWMA data folder (`<data dir>/planner/planner.sqlite3`, the same
per-user folder as memory; outside the repository and outside every approved folder, so the
PC index never reads it). No cloud calendar.

Nothing is ever erased: "done" and "cancelled" are statuses, the row stays. Times are local
wall-clock times stored as ISO text (`YYYY-MM-DDTHH:MM`).
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from memory.config import resolve_data_dir

Kind = Literal["reminder", "todo", "deadline"]
Status = Literal["open", "done", "cancelled"]
DB_NAME = "planner.sqlite3"
NOTIFY_KINDS = ("reminder", "deadline")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('reminder', 'todo', 'deadline')),
    title TEXT NOT NULL,
    due TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'cancelled')),
    created TEXT NOT NULL,
    closed TEXT,
    notified TEXT
);
CREATE INDEX IF NOT EXISTS items_due ON items (status, due);
"""


class PlannerItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    kind: Kind
    title: str
    due: str | None = None  # local time, ISO "YYYY-MM-DDTHH:MM"
    status: Status = "open"


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M")


def planner_db_path(data_dir: Path | None = None) -> Path:
    return (data_dir or resolve_data_dir()) / "planner" / DB_NAME


class PlannerStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._ready = False  # the file is made on first use, not when the catalog is built

    @classmethod
    def default(cls) -> PlannerStore:
        return cls(planner_db_path())

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if not self._ready:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                if not self._ready:
                    conn.executescript(_SCHEMA)
                    self._ready = True
                yield conn
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _item(row: sqlite3.Row) -> PlannerItem:
        return PlannerItem(
            id=row["id"], kind=row["kind"], title=row["title"], due=row["due"], status=row["status"]
        )

    def add(self, kind: Kind, title: str, due: datetime | None, now: datetime) -> PlannerItem:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO items (kind, title, due, created) VALUES (?, ?, ?, ?)",
                (kind, title, _iso(due) if due else None, _iso(now)),
            )
            row = conn.execute("SELECT * FROM items WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return self._item(row)

    def get(self, item_id: int) -> PlannerItem | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return self._item(row) if row else None

    def items(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        kind: Kind | None = None,
        include_closed: bool = False,
        limit: int = 50,
    ) -> list[PlannerItem]:
        """Items due in [start, end) (to-dos without a date are included unless a range is
        given), soonest first; undated to-dos last."""
        sql = "SELECT * FROM items WHERE 1"
        params: list[object] = []
        if not include_closed:
            sql += " AND status = 'open'"
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        if start is not None:
            sql += " AND due >= ?"
            params.append(_iso(start))
        if end is not None:
            sql += " AND due < ?"
            params.append(_iso(end))
        sql += " ORDER BY due IS NULL, due, id LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            return [self._item(r) for r in conn.execute(sql, params)]

    def close(self, item_id: int, status: Status, now: datetime) -> PlannerItem | None:
        """Mark an open item done or cancelled. None if there is no such open item."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE items SET status = ?, closed = ? WHERE id = ? AND status = 'open'",
                (status, _iso(now), item_id),
            )
            if cursor.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return self._item(row)

    def take_due(self, now: datetime, limit: int = 5) -> list[PlannerItem]:
        """Open reminders and deadlines that are due and not yet shown; marks them shown.
        Reminders missed while HCWMA was off come out the next time this runs."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM items WHERE status = 'open' AND notified IS NULL "
                "AND kind IN (?, ?) AND due IS NOT NULL AND due <= ? ORDER BY due LIMIT ?",
                (*NOTIFY_KINDS, _iso(now), limit),
            ).fetchall()
            conn.executemany(
                "UPDATE items SET notified = ? WHERE id = ?", [(_iso(now), r["id"]) for r in rows]
            )
        return [self._item(r) for r in rows]
