"""Files, hashes and the owner-only approval state of generated tools (build step 13).

A generated tool's code lives in `integrations/generated/<name>/` (three files, nothing
else). Whether it may run is NOT written there. The state file in the HCWMA data folder
(outside the repository, like memory) records, per tool and per exact content hash:

    tested     the sandbox test result for this hash
    submitted  the Tool Builder asked for a review of this hash
    approved   the owner approved this hash, and which agents may use it
    disabled   the owner switched the tool off

Status is computed, never stored: a tool is `approved` only while its current files hash to
the approved hash. Any changed byte in the manifest, the code or the tests, or an extra
file, gives a different hash, so the tool is `quarantined` again at once.

Only the owner's review service (`review.py`, reached by the token-protected backend) calls
`approve`, `disable`, `enable` and `rollback`. No tool exposed to an agent does.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from integrations.generated_tools import static_check
from integrations.generated_tools.manifest import (
    ADAPTER_FILE,
    MANIFEST_FILE,
    MAX_FILE_BYTES,
    TEST_FILE,
    TOOL_FILES,
    GeneratedManifest,
    ManifestError,
    generated_root,
    parse_manifest,
)
from integrations.generated_tools.static_check import content_hash
from integrations.mcp.tool_schemas import ToolStatus
from memory.config import resolve_data_dir

STATE_DIR = "generated_tools"
STATE_FILE = "state.json"
SNAPSHOTS = "snapshots"
KEEP_SNAPSHOTS = 5

_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_link(path: Path) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return True
    return path.is_symlink() or bool(
        getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


@dataclass(frozen=True)
class ToolFiles:
    """A snapshot of one tool folder, read once. Everything later is decided from these bytes."""

    name: str
    folder: Path
    files: dict[str, bytes]
    digest: str
    manifest: GeneratedManifest | None
    problems: tuple[str, ...]  # why it cannot be used at all (manifest, extra files, static check)

    @property
    def ok(self) -> bool:
        return self.manifest is not None and not self.problems

    def text(self, filename: str) -> str:
        return self.files.get(filename, b"").decode("utf-8", errors="replace")


def read_tool(name: str, root: Path | None = None) -> ToolFiles | None:
    """Read `root/<name>` as one snapshot, or None if there is no such folder."""
    root = root or generated_root()
    folder = root / name
    if not folder.is_dir() or _is_link(folder):
        return None
    problems: list[str] = []
    files: dict[str, bytes] = {}
    for entry in sorted(folder.iterdir()):
        if entry.name == "__pycache__":
            continue
        if entry.name not in TOOL_FILES or not entry.is_file() or _is_link(entry):
            problems.append(f"unexpected item '{entry.name}' (only {', '.join(TOOL_FILES)})")
            continue
        size = entry.stat().st_size
        if size > MAX_FILE_BYTES[entry.name]:
            problems.append(f"{entry.name} is too large ({size} bytes)")
            continue
        files[entry.name] = entry.read_bytes()
    for needed in TOOL_FILES:
        if needed not in files:
            problems.append(f"{needed} is missing")
    manifest: GeneratedManifest | None = None
    if MANIFEST_FILE in files:
        try:
            manifest = parse_manifest(files[MANIFEST_FILE].decode("utf-8"))
            if manifest.name != name:
                problems.append("the manifest name must equal the folder name")
        except (ManifestError, UnicodeDecodeError) as exc:
            problems.append(str(exc))
    if ADAPTER_FILE in files and TEST_FILE in files:
        try:
            problems += static_check.check_tool(
                files[ADAPTER_FILE].decode("utf-8"), files[TEST_FILE].decode("utf-8")
            )
        except UnicodeDecodeError:
            problems.append("code files must be UTF-8 text")
    return ToolFiles(name, folder, files, content_hash(files), manifest, tuple(problems))


def list_tool_names(root: Path | None = None) -> list[str]:
    root = root or generated_root()
    if not root.is_dir():
        return []
    return sorted(
        p.name
        for p in root.iterdir()
        if p.is_dir() and not p.name.startswith(("_", ".")) and not _is_link(p)
    )


@dataclass
class ToolState:
    tested: dict[str, Any] | None = None  # {hash, passed, at, summary}
    submitted: dict[str, Any] | None = None  # {hash, at}
    approved: dict[str, Any] | None = None  # {hash, at, agents, version}
    approved_history: list[dict[str, Any]] = field(default_factory=list)
    disabled: bool = False


class GeneratedStore:
    """State file + snapshots. Cheap to create: every call re-reads the file, so the gateway
    and the tool server (two processes) always agree."""

    def __init__(self, root: Path | None = None, data_dir: Path | None = None) -> None:
        self.root = root or generated_root()
        self.base = (data_dir or resolve_data_dir()) / STATE_DIR

    @property
    def _path(self) -> Path:
        return self.base / STATE_FILE

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, Any]) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.base, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        Path(tmp).replace(self._path)

    def state(self, name: str) -> ToolState:
        raw = self._load().get(name, {})
        return ToolState(
            tested=raw.get("tested"),
            submitted=raw.get("submitted"),
            approved=raw.get("approved"),
            approved_history=list(raw.get("approved_history", [])),
            disabled=bool(raw.get("disabled", False)),
        )

    def _update(self, name: str, **changes: Any) -> None:
        with _lock:
            data = self._load()
            entry = data.setdefault(name, {})
            for key, value in changes.items():
                if value is None:
                    entry.pop(key, None)
                else:
                    entry[key] = value
            self._save(data)

    # -- status -----------------------------------------------------------------------------

    def status_of(self, tool: ToolFiles) -> tuple[ToolStatus, str]:
        """(status, one-line reason). `approved` only for an exact, clean, approved hash."""
        state = self.state(tool.name)
        if state.disabled:
            return "disabled", "switched off by the owner"
        if not tool.ok:
            return "quarantined", "; ".join(tool.problems[:3]) or "cannot be loaded"
        if state.approved and state.approved.get("hash") == tool.digest:
            return "approved", "approved by the owner"
        if state.approved:
            return "quarantined", "changed since the owner approved it"
        return "quarantined", "waiting for the owner's approval"

    def granted_agents(self, tool: ToolFiles) -> list[str]:
        state = self.state(tool.name)
        if state.approved and state.approved.get("hash") == tool.digest and not state.disabled:
            return list(state.approved.get("agents", []))
        return []

    # -- steps the Tool Builder's own tools may record (about THIS hash only) ----------------

    def record_test(self, name: str, digest: str, passed: bool, summary: str) -> None:
        self._update(
            name, tested={"hash": digest, "passed": passed, "at": _now(), "summary": summary[:600]}
        )

    def record_submitted(self, name: str, digest: str) -> None:
        self._update(name, submitted={"hash": digest, "at": _now()})

    # -- owner decisions (called by review.py only) ------------------------------------------

    def approve(self, tool: ToolFiles, agents: list[str]) -> None:
        state = self.state(tool.name)
        record = {
            "hash": tool.digest,
            "at": _now(),
            "agents": sorted(set(agents)),
            "version": tool.manifest.version if tool.manifest else "?",
        }
        history = [h for h in state.approved_history if h.get("hash") != tool.digest]
        history.append(record)
        self._snapshot(tool)
        self._update(tool.name, approved=record, approved_history=history[-KEEP_SNAPSHOTS:],
                     disabled=None)  # fmt: skip

    def set_disabled(self, name: str, disabled: bool) -> None:
        self._update(name, disabled=True if disabled else None)

    def revoke(self, name: str) -> None:
        self._update(name, approved=None)

    # -- snapshots for rollback --------------------------------------------------------------

    def _snapshot(self, tool: ToolFiles) -> None:
        target = self.base / SNAPSHOTS / tool.name / tool.digest
        if target.exists():
            return
        target.mkdir(parents=True)
        for filename, data in tool.files.items():
            (target / filename).write_bytes(data)

    def snapshot_files(self, name: str, digest: str) -> dict[str, bytes] | None:
        folder = self.base / SNAPSHOTS / name / digest
        if not folder.is_dir():
            return None
        files = {f: (folder / f).read_bytes() for f in TOOL_FILES if (folder / f).is_file()}
        return files if content_hash(files) == digest else None  # a tampered snapshot is useless

    def restore_files(self, name: str, files: dict[str, bytes]) -> None:
        """Put snapshot files back into the tool folder (rollback). Existing files are
        replaced; the folder keeps exactly the three tool files."""
        folder = self.root / name
        folder.mkdir(parents=True, exist_ok=True)
        for entry in folder.iterdir():
            if entry.name not in files and entry.is_file() and not _is_link(entry):
                _to_recycle_bin(entry)
        for filename, data in files.items():
            (folder / filename).write_bytes(data)


def _to_recycle_bin(path: Path) -> None:
    from send2trash import send2trash

    send2trash(str(path))


__all__ = [
    "GeneratedStore",
    "ToolFiles",
    "ToolState",
    "content_hash",
    "list_tool_names",
    "read_tool",
]
