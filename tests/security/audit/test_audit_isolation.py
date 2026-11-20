"""Tests never write the owner's real audit log (found during step 14: `python -m memory` and
`python -m backend.reliability`, run by tests, appended to security/audit/logs/audit.jsonl)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from security.audit.audit_logger import (
    AUDIT_PATH_ENV,
    DEFAULT_AUDIT_PATH,
    get_audit_logger,
    resolve_audit_path,
)


def _events(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_each_test_has_its_own_audit_file(tmp_path: Path) -> None:
    path = resolve_audit_path()
    assert path != DEFAULT_AUDIT_PATH
    assert path.is_relative_to(tmp_path)
    assert get_audit_logger().path == path


def test_the_shared_logger_follows_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = get_audit_logger()
    monkeypatch.setenv(AUDIT_PATH_ENV, str(tmp_path / "other.jsonl"))
    second = get_audit_logger()
    assert second is not first and second.path == tmp_path / "other.jsonl"


def test_without_the_override_the_default_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(AUDIT_PATH_ENV)
    assert resolve_audit_path() == DEFAULT_AUDIT_PATH


def test_memory_cli_audits_into_the_test_file() -> None:
    from memory.__main__ import main

    assert main(["cleanup"]) == 0
    events = _events(resolve_audit_path())
    assert [e for e in events if e["event"] == "memory" and e["tool"] == "cleanup"]


def test_reliability_cli_audits_into_the_test_file() -> None:
    from backend.reliability.__main__ import main

    assert main(["backup"]) == 0
    events = _events(resolve_audit_path())
    assert [e for e in events if e["event"] == "reliability" and e["tool"] == "backup"]
