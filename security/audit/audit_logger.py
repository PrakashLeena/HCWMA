"""Audit log: one JSON line per tool call and per delegation (rule 7, master document 9.5),
plus one per model call with its timings only.

Records hold a short argument summary only: secret-looking keys are redacted, strings are
truncated and collections are reduced to their length, so file contents and secrets do not
end up in the log. Default location `security/audit/logs/audit.jsonl` is gitignored and is a
protected path (no agent tool can read or change it). `HCWMA_AUDIT_PATH` moves it: the test
suite sets it per test (tests/conftest.py) so tests never write the owner's real audit log.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AUDIT_PATH = REPO_ROOT / "security" / "audit" / "logs" / "audit.jsonl"
AUDIT_PATH_ENV = "HCWMA_AUDIT_PATH"


def resolve_audit_path() -> Path:
    """The audit log in use: `HCWMA_AUDIT_PATH` when set (tests), else the default."""
    override = os.environ.get(AUDIT_PATH_ENV)
    return Path(override) if override else DEFAULT_AUDIT_PATH

_SECRET_KEY = re.compile(r"pass|secret|token|key|credential|auth|cookie|session", re.IGNORECASE)
_MAX_STR = 80
# Message text (an email body) and document or file text (`content`, step 15) are the
# owner's private content: the log keeps only their length.
_CONTENT_KEY = re.compile(r"^(body|content)$", re.IGNORECASE)


def _summarize_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= _MAX_STR:
            return value
        return value[:_MAX_STR] + f"…(+{len(value) - _MAX_STR})"
    if isinstance(value, (list, tuple, set, frozenset)):
        return f"<list len={len(value)}>"
    if isinstance(value, dict):
        return f"<dict keys={len(value)}>"
    return f"<{type(value).__name__}>"


def summarize_args(args: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key, value in args.items():
        if _SECRET_KEY.search(str(key)):
            summary[str(key)] = "<redacted>"
        elif _CONTENT_KEY.match(str(key)) and isinstance(value, str):
            summary[str(key)] = f"<text {len(value)} chars>"
        else:
            summary[str(key)] = _summarize_value(value)
    return summary


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class AuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ts: str = Field(default_factory=_now)
    event: Literal[
        "tool_call",
        "delegation",
        "approval",
        "stop",
        "memory",
        "generated_tool",
        "model_call",
        "recovery",
        "reliability",
    ]
    task_id: str
    parent_task_id: str | None = None
    agent: str
    tool: str | None = None
    target_agent: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    outcome: str
    reason: str = ""
    duration_ms: float | None = None


class AuditLogger:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or resolve_audit_path()
        self._lock = threading.Lock()

    def write(self, record: AuditRecord) -> None:
        line = record.model_dump_json() + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line)

    def tool_call(
        self,
        *,
        task_id: str,
        agent: str,
        tool: str,
        args: dict[str, Any],
        outcome: str,
        reason: str = "",
        duration_ms: float | None = None,
    ) -> AuditRecord:
        record = AuditRecord(
            event="tool_call",
            task_id=task_id,
            agent=agent,
            tool=tool,
            args=summarize_args(args),
            outcome=outcome,
            reason=reason[:300],
            duration_ms=None if duration_ms is None else round(duration_ms, 2),
        )
        self.write(record)
        return record

    def delegation(
        self,
        *,
        task_id: str,
        parent_task_id: str | None,
        from_agent: str,
        to_agent: str,
        outcome: str = "delegated",
        reason: str = "",
    ) -> AuditRecord:
        record = AuditRecord(
            event="delegation",
            task_id=task_id,
            parent_task_id=parent_task_id,
            agent=from_agent,
            target_agent=to_agent,
            outcome=outcome,
            reason=reason[:300],
        )
        self.write(record)
        return record

    def approval(
        self,
        *,
        task_id: str,
        agent: str,
        tool: str,
        args: dict[str, Any],
        outcome: str,
        reason: str = "",
    ) -> AuditRecord:
        """An approval prompt and what became of it (requested, approved, denied, …)."""
        record = AuditRecord(
            event="approval",
            task_id=task_id,
            agent=agent,
            tool=tool,
            args=summarize_args(args),
            outcome=outcome,
            reason=reason[:300],
        )
        self.write(record)
        return record

    def stop(self, *, task_ids: list[str], approvals_cancelled: int) -> AuditRecord:
        """The owner pressed "Stop everything"."""
        record = AuditRecord(
            event="stop",
            task_id=",".join(task_ids)[:300] or "-",
            agent="owner",
            outcome="stopped",
            reason=f"{len(task_ids)} task(s) halted, {approvals_cancelled} approval(s) withdrawn",
        )
        self.write(record)
        return record

    def recovery(self, *, task_id: str, outcome: str, reason: str = "") -> AuditRecord:
        """A task left running by a crash or a PC sleep, marked interrupted (step 18)."""
        record = AuditRecord(
            event="recovery", task_id=task_id, agent="hcwma", outcome=outcome, reason=reason[:300]
        )
        self.write(record)
        return record

    def reliability(self, *, action: str, outcome: str, reason: str = "") -> AuditRecord:
        """An owner reliability action (pause model, backup, restore, settings; step 18)."""
        record = AuditRecord(
            event="reliability",
            task_id="-",
            agent="owner",
            tool=action,
            outcome=outcome,
            reason=reason[:300],
        )
        self.write(record)
        return record

    def memory(
        self,
        *,
        action: str,
        outcome: str,
        agent: str = "system",
        task_id: str | None = None,
        reason: str = "",
        details: dict[str, Any] | None = None,
    ) -> AuditRecord:
        """A change to long-term memory (write, refusal, delete, clean-up, backup, restore,
        export). `details` holds ids, scopes and counts only — never the remembered text."""
        record = AuditRecord(
            event="memory",
            task_id=task_id or "-",
            agent=agent,
            tool=action,
            args=summarize_args(details or {}),
            outcome=outcome,
            reason=reason[:300],
        )
        self.write(record)
        return record

    def model_call(
        self,
        *,
        task_id: str,
        agent: str,
        duration_ms: float,
        timings: dict[str, float | int],
        outcome: str = "ok",
    ) -> AuditRecord:
        """One inference through the model queue, for the time split of a request
        (`python -m agents.router.timing`). `timings` holds numbers only (prompt and output
        length in tokens, model load, prompt reading and writing seconds), never text."""
        record = AuditRecord(
            event="model_call",
            task_id=task_id,
            agent=agent,
            args={k: v for k, v in timings.items() if isinstance(v, (int, float))},
            outcome=outcome,
            duration_ms=round(duration_ms, 2),
        )
        self.write(record)
        return record

    def generated_tool(
        self,
        *,
        action: str,
        tool: str,
        outcome: str,
        agent: str = "system",
        task_id: str | None = None,
        reason: str = "",
        details: dict[str, Any] | None = None,
    ) -> AuditRecord:
        """A step in the life of a Tool Builder tool: scaffold, sandbox test, submit, owner
        approve / disable / rollback (build step 13). `details` holds names, hashes and counts,
        never code."""
        record = AuditRecord(
            event="generated_tool",
            task_id=task_id or "-",
            agent=agent,
            tool=tool,
            args=summarize_args({"action": action, **(details or {})}),
            outcome=outcome,
            reason=reason[:300],
        )
        self.write(record)
        return record

    def prune(self, older_than_days: int) -> int:
        """Retention (memory policy section 6): drop records older than the limit.
        Returns how many were removed. Lines that cannot be read are kept."""
        if not self.path.exists():
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat(
            timespec="milliseconds"
        )
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
            kept: list[str] = []
            for line in lines:
                try:
                    old = json.loads(line)["ts"] < cutoff
                except (ValueError, KeyError, TypeError):
                    old = False
                if not old:
                    kept.append(line)
            removed = len(lines) - len(kept)
            if removed:
                tmp = self.path.with_suffix(".jsonl.tmp")
                tmp.write_text("".join(kept), encoding="utf-8")
                tmp.replace(self.path)
        return removed

    def read_all(self) -> list[AuditRecord]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as f:
            return [AuditRecord.model_validate(json.loads(line)) for line in f if line.strip()]


def get_audit_logger() -> AuditLogger:
    """One shared logger per audit file. Keyed by path, so a test that moved the log through
    `HCWMA_AUDIT_PATH` never gets a logger cached for the real file."""
    return _logger_for(resolve_audit_path())


@lru_cache(maxsize=8)
def _logger_for(path: Path) -> AuditLogger:
    return AuditLogger(path)
