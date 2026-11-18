"""Keep the PC file index fresh while HCWMA runs (build step 8).

`IndexService.start()` does one full scan, then watches every indexed root with `watchdog`
and applies each change to the index. The same rules as the scanner apply to every event
(`entry_for` / `allowed_dir`), so a file under a protected path is never added.
"""

from __future__ import annotations

import logging
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from memory.pc_index.index import FileIndex
from memory.pc_index.scanner import (
    ScanReport,
    entry_for,
    folder_entry_for,
    has_skipped_part,
    index_roots,
    root_of,
    scan,
    scan_tree,
)
from security.permissions.path_check import PathPolicy

log = logging.getLogger(__name__)


class _Handler(FileSystemEventHandler):
    def __init__(self, service: IndexService) -> None:
        self._service = service

    def on_created(self, event: FileSystemEvent) -> None:
        self._service.refresh(Path(str(event.src_path)))

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:  # a folder's own mtime change carries no file news
            self._service.refresh(Path(str(event.src_path)))

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._service.forget(Path(str(event.src_path)))

    def on_moved(self, event: FileSystemEvent) -> None:
        self._service.forget(Path(str(event.src_path)))
        self._service.refresh(Path(str(event.dest_path)))


class IndexService:
    def __init__(self, index: FileIndex, policy: PathPolicy) -> None:
        self.index = index
        self.policy = policy
        self.roots: list[Path] = []
        self._observer: BaseObserver | None = None

    def start(self) -> ScanReport:
        """Scan once, then watch. Safe to call when no folder is approved (watches nothing)."""
        report = scan(self.index, self.policy)
        self.roots = [Path(r) for r in report.roots]
        observer = Observer()
        handler = _Handler(self)
        for root in self.roots:
            observer.schedule(handler, str(root), recursive=True)
        observer.daemon = True
        observer.start()
        self._observer = observer
        return report

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    def refresh(self, path: Path) -> None:
        """A file or folder appeared or changed: index it if the rules allow."""
        try:
            roots = self.roots or index_roots(self.policy)
            root = root_of(path, roots)
            if root is None or has_skipped_part(path, root):
                return
            if path.is_dir():
                folder = folder_entry_for(path, root, self.policy)
                if folder is not None:
                    self.index.upsert([folder])
                scan_tree(self.index, self.policy, path, root)
                return
            entry = entry_for(path, root, self.policy)
            if entry is None:
                self.index.remove(path)
            else:
                self.index.upsert([entry])
        except Exception:  # the watcher thread must survive any single bad event
            log.exception("index refresh failed")

    def forget(self, path: Path) -> None:
        try:
            self.index.remove(path)
        except Exception:
            log.exception("index removal failed")
