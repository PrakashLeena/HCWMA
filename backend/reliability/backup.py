"""Full backups of everything HCWMA keeps, and restore (build step 18).

One backup is a folder `<data dir>/backups/full-YYYYMMDD-HHMMSS/` holding:

    memory/memory.sqlite3        the memory database (consistent SQLite copy)
    planner/planner.sqlite3      reminders, deadlines, to-dos
    generated_tools/...          Tool Builder approval state and snapshots (not `run/`)
    settings.json                the owner's reliability settings
    configs/*.yaml               the repository's configs (permissions, model, backend, ...)
    manifest.json                what is inside, with a SHA-256 for each non-database file

The newest `keep` (7) backups are kept; older ones go to the Recycle Bin (rule 9). The
backend makes one whenever the newest is a day old (checked every hour, so a laptop that
was off or asleep catches up). `python -m backend.reliability backup` makes one now.

Restore checks the backup first (databases must open and be the right kind; other files must
match their checksum; only the names above are accepted), backs up the current state, then
copies each part into place. HCWMA must not be running while it restores.

Secrets are never in a backup: they live in the OS credential store (rule 11). Memory
entries the owner forgets are also removed from the memory copy in every full backup
(`MemoryService` purges them), as they are from the memory-only backups.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

from pydantic import BaseModel
from send2trash import send2trash

from memory.config import REPO_ROOT, backups_dir, resolve_data_dir
from memory.storage.store import MemoryStore, NotAMemoryDatabase, check_is_memory_database

FULL_PREFIX = "full-"
MANIFEST = "manifest.json"
DEFAULT_KEEP = 7
DEFAULT_EVERY_HOURS = 24
CONFIGS_DIR = REPO_ROOT / "configs"

MEMORY_DB = "memory/memory.sqlite3"
PLANNER_DB = "planner/planner.sqlite3"
DATABASES = (MEMORY_DB, PLANNER_DB)
TOOLS_DIR = "generated_tools"
TOOLS_SKIP = {"run"}  # temporary run folders, not state
SETTINGS = "settings.json"
CONFIG_PREFIX = "configs"


class BackupRefused(ValueError):
    """The backup folder is missing, damaged or not an HCWMA backup. Nothing was changed."""


class BackupItem(BaseModel):
    path: str  # relative, forward slashes
    kind: str  # "database" | "file"
    sha256: str = ""  # files only (a forget may legitimately change the memory copy)
    size: int = 0


class BackupManifest(BaseModel):
    format: int = 1
    created: str
    label: str = ""
    items: list[BackupItem]


class RestoreReport(BaseModel):
    restored: list[str]
    safety_backup: str | None
    memory_entries: int | None = None
    planner_items: int | None = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_sqlite(src: Path, dest: Path) -> None:
    """A consistent copy even while the database is open elsewhere."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True)
    out = sqlite3.connect(dest)
    try:
        source.backup(out)
        out.execute("PRAGMA journal_mode = DELETE")
        out.commit()
    finally:
        source.close()
        out.close()


def _allowed(rel: str) -> bool:
    """Only the fixed parts of a backup are ever read or restored (no path tricks)."""
    parts = PurePosixPath(rel).parts
    if not parts or rel.startswith("/") or any(p in ("..", "", ".") or ":" in p for p in parts):
        return False
    if rel in DATABASES or rel == SETTINGS:
        return True
    if parts[0] == TOOLS_DIR and len(parts) >= 2 and parts[1] not in TOOLS_SKIP:
        return True
    return parts[0] == CONFIG_PREFIX and len(parts) == 2 and parts[1].endswith(".yaml")


def _planner_count(path: Path) -> int:
    try:
        con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupRefused("the planner database in the backup is damaged")
            return int(con.execute("SELECT count(*) FROM items").fetchone()[0])
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise BackupRefused("the planner file in the backup is not a planner database") from exc


class BackupManager:
    def __init__(
        self,
        data_dir: Path | None = None,
        configs_dir: Path | None = None,
        keep: int = DEFAULT_KEEP,
        clock: type[datetime] = datetime,
    ) -> None:
        self.data_dir = data_dir or resolve_data_dir()
        self.configs_dir = configs_dir or CONFIGS_DIR
        self.backup_root = backups_dir(self.data_dir)
        self.keep = keep
        self._clock = clock

    # ---------- listing ----------

    def backups(self) -> list[Path]:
        """Full backups, oldest first (only folders with a manifest count)."""
        if not self.backup_root.is_dir():
            return []
        return sorted(
            p for p in self.backup_root.glob(f"{FULL_PREFIX}*") if (p / MANIFEST).is_file()
        )

    def newest_time(self) -> datetime | None:
        found = self.backups()
        if not found:
            return None
        return datetime.fromtimestamp((found[-1] / MANIFEST).stat().st_mtime)

    def due(self, every_hours: int = DEFAULT_EVERY_HOURS) -> bool:
        newest = self.newest_time()
        return newest is None or self._clock.now() - newest >= timedelta(hours=every_hours)

    # ---------- backup ----------

    def _sources(self) -> list[tuple[str, Path, str]]:
        """(relative name, source path, kind) for everything that exists now."""
        found: list[tuple[str, Path, str]] = []
        for rel in DATABASES:
            src = self.data_dir / rel
            if src.is_file():
                found.append((rel, src, "database"))
        tools = self.data_dir / TOOLS_DIR
        if tools.is_dir():
            for src in sorted(tools.rglob("*")):
                rel = src.relative_to(self.data_dir).as_posix()
                if src.is_file() and not src.is_symlink() and _allowed(rel):
                    found.append((rel, src, "file"))
        settings = self.data_dir / SETTINGS
        if settings.is_file():
            found.append((SETTINGS, settings, "file"))
        if self.configs_dir.is_dir():
            for src in sorted(self.configs_dir.glob("*.yaml")):
                found.append((f"{CONFIG_PREFIX}/{src.name}", src, "file"))
        return found

    def backup(self, label: str = "") -> Path:
        stamp = self._clock.now().strftime("%Y%m%d-%H%M%S")
        dest = self.backup_root / f"{FULL_PREFIX}{stamp}"
        n = 1
        while dest.exists():
            n += 1
            dest = self.backup_root / f"{FULL_PREFIX}{stamp}-{n}"
        dest.mkdir(parents=True)
        items: list[BackupItem] = []
        for rel, src, kind in self._sources():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if kind == "database":
                _copy_sqlite(src, target)
                items.append(BackupItem(path=rel, kind=kind, size=target.stat().st_size))
            else:
                shutil.copy2(src, target)
                items.append(
                    BackupItem(
                        path=rel, kind=kind, sha256=_sha256(target), size=target.stat().st_size
                    )
                )
        manifest = BackupManifest(
            created=self._clock.now().isoformat(timespec="seconds"), label=label, items=items
        )
        # Written last: a folder without a manifest is an unfinished backup and is ignored.
        (dest / MANIFEST).write_text(manifest.model_dump_json(indent=1), encoding="utf-8")
        self.prune()
        return dest

    def prune(self) -> list[Path]:
        """Keep the newest `keep`; older ones go to the Recycle Bin, never erased."""
        old = self.backups()[: -self.keep] if self.keep > 0 else []
        for folder in old:
            send2trash(str(folder))
        return old

    # ---------- restore ----------

    def check(self, folder: Path) -> BackupManifest:
        """Raise `BackupRefused` unless `folder` is a complete, undamaged HCWMA backup."""
        folder = folder.resolve()
        if not (folder / MANIFEST).is_file():
            raise BackupRefused(f"'{folder}' is not an HCWMA full backup (no {MANIFEST})")
        try:
            manifest = BackupManifest.model_validate_json(
                (folder / MANIFEST).read_text(encoding="utf-8")
            )
        except ValueError as exc:
            raise BackupRefused("the backup's manifest is damaged") from exc
        for item in manifest.items:
            if not _allowed(item.path):
                raise BackupRefused(f"the backup names a part HCWMA never writes: {item.path!r}")
            src = folder / item.path
            if src.is_symlink() or not src.is_file() or folder not in src.resolve().parents:
                raise BackupRefused(f"the backup is missing {item.path}")
            if item.kind == "file" and _sha256(src) != item.sha256:
                raise BackupRefused(f"{item.path} in the backup is damaged (checksum differs)")
            if item.path == MEMORY_DB:
                try:
                    check_is_memory_database(src)
                except NotAMemoryDatabase as exc:
                    raise BackupRefused(str(exc)) from exc
            if item.path == PLANNER_DB:
                _planner_count(src)
        return manifest

    def restore(self, folder: Path, *, safety_backup: bool = True) -> RestoreReport:
        """Put a backup back. The current state is backed up first (if there is any)."""
        manifest = self.check(folder)
        folder = folder.resolve()
        safety = None
        if safety_backup and self._sources():
            safety = str(self.backup(label=f"before restoring {folder.name}"))
        restored: list[str] = []
        memory_entries = planner_items = None
        for item in manifest.items:
            src = folder / item.path
            if item.path.startswith(f"{CONFIG_PREFIX}/"):
                dest = self.configs_dir / PurePosixPath(item.path).name
            else:
                dest = self.data_dir / item.path
            dest.parent.mkdir(parents=True, exist_ok=True)
            if item.path == MEMORY_DB:
                MemoryStore(dest).restore_from(src)
                memory_entries = check_is_memory_database(dest)
            elif item.path == PLANNER_DB:
                _copy_sqlite(src, dest)
                planner_items = _planner_count(dest)
            else:
                shutil.copy2(src, dest)  # replaces the current copy of this one file
            restored.append(item.path)
        return RestoreReport(
            restored=restored,
            safety_backup=safety,
            memory_entries=memory_entries,
            planner_items=planner_items,
        )


def load_manifest(folder: Path) -> BackupManifest | None:
    try:
        return BackupManifest.model_validate(json.loads((folder / MANIFEST).read_text("utf-8")))
    except (OSError, ValueError):
        return None
