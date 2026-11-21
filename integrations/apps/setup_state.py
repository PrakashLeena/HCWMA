"""First-run setup state and the approved-folders step of the wizard (owner decision 2026-11-21).

`setup.json` in the HCWMA data folder records that the owner finished (or skipped) the wizard.
An install that existed before adapters could be switched off keeps its adapters on (written
once, audited), so an update never silently turns off something the owner already used.

Folder detection reads where Windows really keeps Desktop, Documents and Downloads (OneDrive
may redirect them) from the per-user "User Shell Folders" registry key. Read-only. Adding a
folder to `approved_folders` happens only on an owner click in the wizard.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from integrations.apps.adapters import ADAPTERS_FILE, BUNDLED, adapters_path, set_adapter
from security.audit.audit_logger import AuditLogger, get_audit_logger
from security.permissions.config import DEFAULT_PERMISSIONS_PATH, load_permissions_config

SETUP_FILE = "setup.json"
_NEW_FILES = frozenset(
    {SETUP_FILE, ADAPTERS_FILE, "app_catalog.json", "app_permissions.json", "activity.json"}
)


def _data_dir() -> Path:
    from memory.config import resolve_data_dir

    return resolve_data_dir()


def setup_path(data_dir: Path | None = None) -> Path:
    return (data_dir or _data_dir()) / SETUP_FILE


def setup_done(data_dir: Path | None = None) -> bool:
    return setup_path(data_dir).exists()


def mark_setup_done(how: str, data_dir: Path | None = None) -> None:
    path = setup_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"done": time.strftime("%Y-%m-%d %H:%M"), "how": how}), "utf-8")
    get_audit_logger().app_access(action="setup", app="-", outcome=how)


def keep_adapters_of_existing_install(
    data_dir: Path | None = None, audit: AuditLogger | None = None
) -> list[str]:
    """Once, on the first start of this version: an install that already had data keeps every
    bundled adapter on (it was usable before). A new install starts with all adapters off and
    the wizard offers them. Returns the adapters turned on."""
    folder = data_dir or _data_dir()
    state = adapters_path(folder)
    if state.exists() or not folder.exists():
        return []
    if not any(p.name not in _NEW_FILES for p in folder.iterdir()):
        return []  # a new install
    for info in BUNDLED:
        set_adapter(
            info.id,
            True,
            source="kept on: in use before adapters could be switched (update 2026-11-21)",
            audit=audit,
            path=state,
        )
    return [a.id for a in BUNDLED]


# ---------- approved folders ----------

_SHELL_FOLDERS = {
    "Desktop": "Desktop",
    "Documents": "Personal",
    "Downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
}


def detect_user_folders() -> dict[str, str]:
    """{"Desktop": path, ...} as Windows really keeps them (OneDrive may redirect)."""
    home = Path.home()
    found = {name: str(home / name) for name in _SHELL_FOLDERS}
    if sys.platform != "win32":
        return found
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            for name, value_name in _SHELL_FOLDERS.items():
                try:
                    raw, _kind = winreg.QueryValueEx(key, value_name)
                except OSError:
                    continue
                found[name] = os.path.expandvars(str(raw))
    except OSError:
        pass
    return found


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def folder_status(permissions_path: Path = DEFAULT_PERMISSIONS_PATH) -> list[dict[str, object]]:
    """Each detected user folder: where it is, whether OneDrive redirects it, whether it is
    already approved."""
    approved = [
        Path(os.path.expanduser(p)).resolve()
        for p in load_permissions_config(permissions_path).approved_folders
    ]
    out: list[dict[str, object]] = []
    for name, path in detect_user_folders().items():
        real = Path(path)
        try:
            real = real.resolve()
        except OSError:
            pass
        out.append(
            {
                "name": name,
                "path": str(real),
                "onedrive": "onedrive" in str(real).lower(),
                "exists": real.is_dir(),
                "approved": any(_same(real, a) or real.is_relative_to(a) for a in approved),
            }
        )
    return out


def _yaml_path(folder: Path) -> str:
    home = Path.home()
    try:
        return "~/" + folder.relative_to(home).as_posix()
    except ValueError:
        return folder.as_posix()


def approve_folder(
    path: str,
    permissions_path: Path = DEFAULT_PERMISSIONS_PATH,
    audit: AuditLogger | None = None,
) -> str:
    """Add one DETECTED user folder (Desktop / Documents / Downloads) to `approved_folders`.
    Only those folders can be added here; anything else is refused."""
    detected = {
        os.path.normcase(str(Path(p).resolve())): Path(p).resolve()
        for p in detect_user_folders().values()
    }
    folder = detected.get(os.path.normcase(str(Path(path).resolve())))
    if folder is None or not folder.is_dir():
        raise ValueError("only a detected Desktop, Documents or Downloads folder can be added here")
    entry = _yaml_path(folder)
    text = permissions_path.read_text(encoding="utf-8")
    if any(
        row["path"] == str(folder) and row["approved"] for row in folder_status(permissions_path)
    ):
        return entry
    match = re.search(r"^approved_folders:\n((?:  - .*\n)+)", text, re.MULTILINE)
    if match is None:
        raise ValueError("could not find approved_folders in permissions.yaml")
    line = (
        f"  - {entry}  # added by the owner in the first-run wizard {time.strftime('%Y-%m-%d')}\n"
    )
    updated = text[: match.end()] + line + text[match.end() :]
    tmp = permissions_path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(updated, encoding="utf-8")
    try:
        load_permissions_config(tmp)  # the new file must still be a valid config
    except Exception:
        tmp.replace(permissions_path.with_suffix(".rejected.tmp"))
        raise ValueError(
            "the updated permissions.yaml would not be valid; nothing changed"
        ) from None
    os.replace(tmp, permissions_path)
    (audit or get_audit_logger()).app_access(
        action="approve_folder", app=entry, outcome="approved", reason="first-run wizard"
    )
    return entry
