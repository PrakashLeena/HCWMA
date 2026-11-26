"""Step 6: audit logger (architecture rule 7, master document 9.5)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from security.audit.audit_logger import AuditLogger, summarize_args


def test_record_is_appended_as_json_line(tmp_path: Path) -> None:
    log = AuditLogger(tmp_path / "logs" / "audit.jsonl")
    log.tool_call(
        task_id="t1",
        agent="tester",
        tool="get_system_time",
        args={},
        outcome="ok",
        reason="",
        duration_ms=1.5,
    )
    records = log.read_all()
    assert len(records) == 1
    rec = records[0]
    assert rec.event == "tool_call"
    assert rec.task_id == "t1"
    assert rec.agent == "tester"
    assert rec.tool == "get_system_time"
    assert rec.outcome == "ok"
    line = (tmp_path / "logs" / "audit.jsonl").read_text(encoding="utf-8").strip()
    assert json.loads(line)["ts"]


def test_delegation_record(tmp_path: Path) -> None:
    log = AuditLogger(tmp_path / "audit.jsonl")
    log.delegation(task_id="t", parent_task_id=None, from_agent="router", to_agent="echo")
    rec = log.read_all()[0]
    assert rec.event == "delegation"
    assert rec.agent == "router"
    assert rec.target_agent == "echo"


def test_secrets_are_redacted_and_long_values_truncated() -> None:
    summary = summarize_args(
        {"password": "hunter2", "api_key": "abc", "query": "x" * 500, "n": 3, "items": [1, 2]}
    )
    assert summary["password"] == "<redacted>"  # noqa: S105 — placeholder, not a password
    assert summary["api_key"] == "<redacted>"
    assert len(summary["query"]) < 100
    assert summary["n"] == 3
    assert summary["items"] == "<list len=2>"
    assert "hunter2" not in json.dumps(summary)


def test_concurrent_writes_do_not_interleave(tmp_path: Path) -> None:
    log = AuditLogger(tmp_path / "audit.jsonl")

    def write(i: int) -> None:
        for j in range(25):
            log.tool_call(task_id=f"{i}-{j}", agent="a", tool="t", args={}, outcome="ok", reason="")

    threads = [threading.Thread(target=write, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(log.read_all()) == 100


def test_model_call_record_holds_numbers_only(tmp_path: Path) -> None:
    log = AuditLogger(tmp_path / "audit.jsonl")
    log.model_call(
        task_id="t",
        agent="router",
        duration_ms=1234.567,
        timings={"prompt_len": 900, "prefill_s": 3.5, "text": "secret prompt"},  # type: ignore[dict-item]
    )
    rec = log.read_all()[0]
    assert (rec.event, rec.agent, rec.task_id, rec.duration_ms) == (
        "model_call",
        "router",
        "t",
        1234.57,
    )
    # never text; which model answered (2026-11-26) is the only string
    assert rec.args == {"prompt_len": 900, "prefill_s": 3.5, "provider": "local", "model": ""}


def test_document_and_file_content_is_logged_as_a_length_only() -> None:
    """Step 15: create_document / write_file text never reaches the audit log."""
    from security.audit.audit_logger import summarize_args

    text = "My private diary entry about the hospital visit."
    assert summarize_args({"name": "notes", "content": text}) == {
        "name": "notes",
        "content": f"<text {len(text)} chars>",
    }
