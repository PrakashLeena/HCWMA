"""Which apps the owner lets HCWMA open without asking (owner decision 2026-11-21).

"Always allow" is saved per app in the HCWMA data folder (`app_permissions.json`, a protected
path for agent tools). Only owner clicks change it: "Always allow" on an approval card, the
Settings page, or the first-run wizard, all behind the per-install token. No agent tool and no
voice route reaches it. It covers OPENING the app only: clicks, typing and sends inside an app
still ask every time. Every change is audited.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from pydantic import BaseModel, Field

from security.audit.audit_logger import AuditLogger, get_audit_logger

ACCESS_FILE = "app_permissions.json"
_lock = threading.Lock()


class AllowedApp(BaseModel):
    id: str
    name: str
    since: str
    source: str  # approval_card | settings | wizard


class AccessFile(BaseModel):
    allowed: dict[str, AllowedApp] = Field(default_factory=dict)


def access_path(data_dir: Path | None = None) -> Path:
    from memory.config import resolve_data_dir

    return (data_dir or resolve_data_dir()) / ACCESS_FILE


def load_access(path: Path | None = None) -> AccessFile:
    try:
        return AccessFile.model_validate_json((path or access_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return AccessFile()


def _save(data: AccessFile, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data.model_dump(), indent=1), encoding="utf-8")
    os.replace(tmp, path)


def is_always_allowed(app_id: str, path: Path | None = None) -> bool:
    return app_id in load_access(path).allowed


def allow_always(
    app_id: str,
    name: str,
    *,
    source: str,
    audit: AuditLogger | None = None,
    path: Path | None = None,
) -> None:
    target = path or access_path()
    with _lock:
        data = load_access(target)
        data.allowed[app_id] = AllowedApp(
            id=app_id, name=name, since=time.strftime("%Y-%m-%d %H:%M"), source=source
        )
        _save(data, target)
    (audit or get_audit_logger()).app_access(
        action="always_allow", app=name, outcome="allowed", reason=f"{source}; id {app_id}"
    )


def revoke_always(
    app_id: str, *, audit: AuditLogger | None = None, path: Path | None = None
) -> bool:
    target = path or access_path()
    with _lock:
        data = load_access(target)
        gone = data.allowed.pop(app_id, None)
        if gone is None:
            return False
        _save(data, target)
    (audit or get_audit_logger()).app_access(
        action="revoke", app=gone.name, outcome="revoked", reason=f"settings; id {app_id}"
    )
    return True
