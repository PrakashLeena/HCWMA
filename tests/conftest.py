"""Shared test setup: no test may read or change the owner's real data.

1. Every test gets its own temp folders, through the environment variables the code reads
   (child processes such as the stdio MCP server inherit them, see `integrations/mcp/client.py`):

   HCWMA_DATA_DIR         memory database, backups, tool approval state, model lock, cache
   HCWMA_GENERATED_DIR    Tool Builder output (instead of integrations/generated/)
   HCWMA_AUDIT_PATH       the audit log (instead of security/audit/logs/audit.jsonl)
   HCWMA_INDEX_PATH       the PC file index (instead of memory/pc_index/data/index.sqlite)
   HCWMA_DISABLE_KEYRING  the Gmail credential store reads nothing and writes nothing

   and the `keyring` module is pointed at an in-memory store (the backend API token).

2. A guard (a Python audit hook) refuses, and fails the test for, any file or database access
   under the real locations: %LOCALAPPDATA%\\HCWMA, the real audit log folder, the real PC
   index folder and the real integrations/generated folder. The access is refused, so a
   test that slips past the variables above cannot damage anything either.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend

from security.audit.audit_logger import get_audit_logger

_REPO = Path(__file__).resolve().parents[1]


def _real_roots() -> tuple[str, ...]:
    roots = [
        _REPO / "security" / "audit" / "logs",
        _REPO / "memory" / "pc_index" / "data",
        _REPO / "integrations" / "generated",
    ]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "HCWMA")
    else:  # the same fallback as memory.config.default_data_dir
        xdg = os.environ.get("XDG_DATA_HOME")
        roots.append((Path(xdg) if xdg else Path.home() / ".local" / "share") / "hcwma")
    return tuple(os.path.normcase(os.path.abspath(r)) for r in roots)


REAL_ROOTS = _real_roots()
# Repo folders (and their subfolders) whose NAMES a repo-wide source scan may list, never
# open: the owner's data
# folder (%LOCALAPPDATA%\HCWMA) is never listed.
_LISTABLE = frozenset(REAL_ROOTS[:3])
_LISTING = frozenset({"os.listdir", "os.scandir"})
_WATCHED = frozenset(
    {
        "open", "sqlite3.connect", "os.remove", "os.rename", "os.replace", "os.mkdir",
        "os.rmdir", "os.listdir", "os.scandir", "os.chmod", "os.utime", "os.truncate",
        "shutil.rmtree", "shutil.copyfile", "shutil.copytree", "shutil.move",
    }
)  # fmt: skip
_state: dict[str, object] = {"active": False, "test": "", "violations": []}


def _under_real_root(value: object, event: str = "") -> str | None:
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    if not isinstance(value, (str, os.PathLike)):
        return None
    text = os.fspath(value)
    if not text or text == ":memory:" or text.startswith("file::memory:"):
        return None
    if text.startswith("file:"):  # sqlite URI
        text = text[5:].split("?", 1)[0]
    try:
        full = os.path.normcase(os.path.abspath(text))
    except (TypeError, ValueError):
        return None
    for root in REAL_ROOTS:
        if full == root or full.startswith(root + os.sep):
            if event in _LISTING and root in _LISTABLE:
                return None
            return full
    return None


def _read_only_source(event: str, args: tuple[object, ...], hit: str) -> bool:
    """Reading or listing the tracked source in the real integrations/generated/ folder is
    fine (repo-wide source scans); writing there is not (tests use HCWMA_GENERATED_DIR)."""
    root = REAL_ROOTS[2]
    if not (hit == root or hit.startswith(root + os.sep)):
        return False
    if event in _LISTING:
        return True
    mode = args[1] if event == "open" and len(args) > 1 else None
    return isinstance(mode, str) and not any(c in mode for c in "wax+")


def _guard(event: str, args: tuple[object, ...]) -> None:
    if not _state["active"] or event not in _WATCHED:
        return
    paths = args[:1] if event in ("open", "sqlite3.connect") else args[:2]
    for arg in paths:
        hit = _under_real_root(arg, event)
        if hit and _read_only_source(event, args, hit):
            continue
        if hit:
            violations = _state["violations"]
            assert isinstance(violations, list)
            violations.append(f"{event} {hit}")
            raise PermissionError(f"test isolation: '{event}' on the owner's real data: {hit}")


sys.addaudithook(_guard)  # cannot be removed; it does nothing outside a running test


class MemoryKeyring(KeyringBackend):
    """An in-memory credential store: the real Windows Credential Manager is never used."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.items.pop((service, username), None)


@pytest.fixture(autouse=True)
def _isolated_hcwma_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[None]:
    data = tmp_path / "hcwma-data"
    monkeypatch.setenv("HCWMA_DATA_DIR", str(data))
    monkeypatch.setenv("HCWMA_GENERATED_DIR", str(tmp_path / "generated"))
    monkeypatch.setenv("HCWMA_AUDIT_PATH", str(tmp_path / "audit" / "audit.jsonl"))
    monkeypatch.setenv("HCWMA_INDEX_PATH", str(tmp_path / "pc-index" / "index.sqlite"))
    monkeypatch.setenv("HCWMA_DISABLE_KEYRING", "1")
    previous = keyring.get_keyring()
    keyring.set_keyring(MemoryKeyring())
    get_audit_logger.cache_clear()  # it is cached per process: resolve the temp path again
    _state.update(active=True, test=request.node.nodeid, violations=[])
    try:
        yield
    finally:
        _state["active"] = False
        keyring.set_keyring(previous)
        get_audit_logger.cache_clear()
    violations = _state["violations"]
    assert isinstance(violations, list)
    if violations:
        shown = "\n  ".join(sorted(set(violations))[:10])
        pytest.fail(f"this test touched the owner's real HCWMA data:\n  {shown}", pytrace=False)
