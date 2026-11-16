"""Audit log: one JSON line per tool call and per delegation (rule 7, master document 9.5).

Records hold a short argument summary only: secret-looking keys are redacted, strings are
truncated and collections are reduced to their length, so file contents and secrets do not
end up in the log. Default location `security/audit/logs/audit.jsonl` is gitignored and is a
protected path (no agent tool can read or change it).
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AUDIT_PATH = REPO_ROOT / "security" / "audit" / "logs" / "audit.jsonl"

_SECRET_KEY = re.compile(r"pass|secret|token|key|credential|auth|cookie|session", re.IGNORECASE)
_MAX_STR = 80


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
    return {
        str(k): "<redacted>" if _SECRET_KEY.search(str(k)) else _summarize_value(v)
        for k, v in args.items()
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class AuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ts: str = Field(default_factory=_now)
    event: Literal["tool_call", "delegation"]
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
    def __init__(self, path: Path = DEFAULT_AUDIT_PATH) -> None:
        self.path = path
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
    ) -> AuditRecord:
        record = AuditRecord(
            event="delegation",
            task_id=task_id,
            parent_task_id=parent_task_id,
            agent=from_agent,
            target_agent=to_agent,
            outcome=outcome,
        )
        self.write(record)
        return record

    def read_all(self) -> list[AuditRecord]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as f:
            return [AuditRecord.model_validate(json.loads(line)) for line in f if line.strip()]


@lru_cache(maxsize=1)
def get_audit_logger() -> AuditLogger:
    return AuditLogger()
