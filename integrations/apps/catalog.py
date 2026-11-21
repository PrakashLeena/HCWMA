"""The app inventory: which apps are installed on this PC (owner decision 2026-11-21).

A scan reads, never changes: the Start menu's app list (`Get-StartApps`: Store and desktop apps
with their AppUserModelIDs), Store packages (`Get-AppxPackage`: publisher, package family),
Start Menu shortcuts (their target program) and the installed-programs registry keys (name,
publisher, program). The merged result is saved per PC in the HCWMA data folder as
`app_catalog.json` (never in Git; a protected path for agent tools).

Blocked apps (`blocklist.py`) stay in the catalog, marked, so the owner can see why they are
never opened.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from integrations.apps.blocklist import RegisteredBrowser, blocked_reason, browser_match

CATALOG_FILE = "app_catalog.json"
SCAN_EVERY_S = 24 * 3600

# Known-folder GUIDs Windows uses in desktop AppIDs ("{GUID}\Folder\app.exe").
_KNOWN_FOLDERS = {
    "{6d809377-6af0-444b-8957-a3773f02200e}": "ProgramW6432",
    "{7c5a40ef-a0fb-4bfc-874a-c0f2e0b9fa8e}": "ProgramFiles(x86)",
    "{1ac14e77-02e7-4e5d-b744-2eb1ae5198b7}": "SystemRoot\\System32",
    "{f38bf404-1d43-42f2-9305-67de0b28fc23}": "SystemRoot",
}


# ---------- what a scan returns (plain data, so tests use a fake list) ----------


class StartApp(BaseModel):
    name: str
    app_id: str


class StorePackage(BaseModel):
    name: str
    publisher: str = ""  # "CN=..., O=..." as Windows gives it
    family: str  # PackageFamilyName, e.g. 5319275A.WhatsAppDesktop_cv1g1gvanyjgm
    framework: bool = False


class Shortcut(BaseModel):
    name: str
    target: str


class InstalledProgram(BaseModel):
    name: str
    publisher: str = ""
    exe: str = ""  # from DisplayIcon, when it is a program


class RawScan(BaseModel):
    start_apps: list[StartApp] = Field(default_factory=list)
    packages: list[StorePackage] = Field(default_factory=list)
    shortcuts: list[Shortcut] = Field(default_factory=list)
    programs: list[InstalledProgram] = Field(default_factory=list)
    browsers: list[RegisteredBrowser] = Field(default_factory=list)  # Windows' web browsers


class AppScanner(Protocol):
    """An OS adapter: reads the lists above. Read-only."""

    def scan(self) -> RawScan: ...


# ---------- the catalog ----------


class CatalogApp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str  # stable per PC: name slug + hash of how it is started
    name: str
    kind: Literal["store", "desktop"]
    app_id: str = ""  # AppUserModelID: opened through the shell's AppsFolder
    exe: str = ""  # program path (desktop apps), when known
    publisher: str = ""
    package_family: str = ""
    sources: list[str] = Field(default_factory=list)  # start_menu, store, shortcut, registry
    blocked: str | None = None  # why HCWMA may never open it

    def process_names(self) -> tuple[str, ...]:
        if not self.exe:
            return ()
        return (PureWindowsPath(self.exe).name.lower().removesuffix(".exe"),)


class Catalog(BaseModel):
    scanned_at: str = ""
    scanned_epoch: float = 0.0
    apps: list[CatalogApp] = Field(default_factory=list)

    def get(self, app_id: str) -> CatalogApp | None:
        return next((a for a in self.apps if a.id == app_id), None)

    def find(self, name: str) -> list[CatalogApp]:
        """Apps the owner may mean by `name`: an exact name first; otherwise every app whose
        name contains all the words (so "Teams" finds "Microsoft Teams")."""
        wanted = norm(name)
        if not wanted:
            return []
        exact = [a for a in self.apps if norm(a.name) == wanted]
        if exact:
            return exact
        want = set(wanted.split())
        return [a for a in self.apps if want <= set(norm(a.name).split())]


def norm(text: str) -> str:
    return " ".join(re.split(r"[^a-z0-9+]+", text.casefold())).strip()


def _slug(name: str) -> str:
    return "-".join(norm(name).split())[:40] or "app"


def _app_key(app_id: str, exe: str) -> str:
    return hashlib.sha1((app_id or exe).casefold().encode("utf-8")).hexdigest()[:10]  # noqa: S324


def publisher_name(raw: str) -> str:
    """ "CN=WhatsApp Inc., O=WhatsApp Inc., C=US" -> "WhatsApp Inc."."""
    for part in ("O", "CN"):
        match = re.search(rf"(?:^|,\s*){part}=(\"[^\"]+\"|[^,]+)", raw)
        if match:
            return match.group(1).strip().strip('"')
    return raw.strip()


def expand_app_id_path(app_id: str) -> str:
    """A desktop AppID that is a program path ("{GUID}\\Notepad++\\notepad++.exe" or
    "C:\\...\\x.exe"), expanded; "" when it is not a path."""
    if not app_id.lower().endswith(".exe"):
        return ""
    head, sep, rest = app_id.partition("\\")
    env = _KNOWN_FOLDERS.get(head.lower())
    if env and sep:
        var, _, sub = env.partition("\\")
        base = os.environ.get(var) or os.environ.get("ProgramFiles", "")
        return str(PureWindowsPath(base, sub, rest)) if base else ""
    if re.match(r"^[A-Za-z]:\\", app_id):
        return app_id
    return ""


def _is_program(target: str) -> bool:
    low = target.lower()
    return low.endswith(".exe") and "unins" not in PureWindowsPath(low).name


# Registry-only entries are a weak source: many point at installers, updaters or runtimes.
_NOT_AN_APP_DIRS = ("\\package cache\\", "\\installer\\", "\\common files\\", "\\windows\\")
_NOT_AN_APP_NAMES = (
    "redistributable", "runtime", "sdk", "driver", "hotfix", "language pack", "setup",
    "install", "update", "helper", "service",
)  # fmt: skip


def _registry_app(name: str, exe: str) -> bool:
    low_exe, low_name = exe.lower(), name.lower()
    exe_name = PureWindowsPath(low_exe).name
    return (
        _is_program(exe)
        and not any(d in low_exe for d in _NOT_AN_APP_DIRS)
        and not any(w in low_name or w in exe_name for w in _NOT_AN_APP_NAMES)
    )


def build_catalog(raw: RawScan, *, windows_dir: str = "", now: float | None = None) -> Catalog:
    """Merge one scan into catalog entries. Pure: no OS calls."""
    packages = {p.family.casefold(): p for p in raw.packages if not p.framework}
    by_name_target = {norm(s.name): s.target for s in raw.shortcuts if _is_program(s.target)}
    programs = {norm(p.name): p for p in raw.programs if p.name}
    apps: dict[str, CatalogApp] = {}
    seen_exes: set[str] = set()

    def add(app: CatalogApp) -> None:
        app.blocked = blocked_reason(
            [app.name, app.publisher],
            app_id=app.app_id or app.package_family,
            exe=app.exe,
            windows_dir=windows_dir,
        ) or browser_match(raw.browsers, name=app.name, app_id=app.app_id, exe=app.exe)
        if app.id not in apps:
            apps[app.id] = app
            if app.exe:
                seen_exes.add(app.exe.casefold())

    for start in raw.start_apps:
        name = start.name.strip()
        if not name or not start.app_id:
            continue
        family, bang, _entry = start.app_id.partition("!")
        if bang:
            pkg = packages.get(family.casefold())
            add(
                CatalogApp(
                    id=f"{_slug(name)}-{_app_key(start.app_id, '')}",
                    name=name,
                    kind="store",
                    app_id=start.app_id,
                    publisher=publisher_name(pkg.publisher) if pkg else "",
                    package_family=family,
                    sources=["start_menu", "store"] if pkg else ["start_menu"],
                )
            )
            continue
        exe = expand_app_id_path(start.app_id) or by_name_target.get(norm(name), "")
        if not exe and not re.match(r"^[\w.\-]{2,120}$", start.app_id):
            continue  # a URL or document shortcut, not an app
        if exe and not _is_program(exe):
            continue
        prog = programs.get(norm(name))
        sources = ["start_menu"] + (["shortcut"] if norm(name) in by_name_target else [])
        add(
            CatalogApp(
                id=f"{_slug(name)}-{_app_key(start.app_id, '')}",
                name=name,
                kind="desktop",
                app_id=start.app_id,
                exe=exe,
                publisher=prog.publisher if prog else "",
                sources=sources + (["registry"] if prog else []),
            )
        )
    for prog in raw.programs:
        exe = prog.exe.split(",")[0].strip().strip('"')
        if not prog.name or not _registry_app(prog.name, exe) or exe.casefold() in seen_exes:
            continue
        if any(norm(a.name) == norm(prog.name) for a in apps.values()):
            continue
        add(
            CatalogApp(
                id=f"{_slug(prog.name)}-{_app_key('', exe)}",
                name=prog.name.strip(),
                kind="desktop",
                exe=exe,
                publisher=prog.publisher,
                sources=["registry"],
            )
        )
    stamp = now if now is not None else time.time()
    return Catalog(
        scanned_at=datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds"),
        scanned_epoch=stamp,
        apps=sorted(apps.values(), key=lambda a: a.name.casefold()),
    )


# ---------- the file ----------


def catalog_path(data_dir: Path | None = None) -> Path:
    from memory.config import resolve_data_dir

    return (data_dir or resolve_data_dir()) / CATALOG_FILE


def load_catalog(path: Path | None = None) -> Catalog:
    try:
        return Catalog.model_validate_json((path or catalog_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Catalog()


def save_catalog(catalog: Catalog, path: Path | None = None) -> Path:
    target = path or catalog_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(catalog.model_dump_json(indent=1), encoding="utf-8")
    os.replace(tmp, target)
    return target


def scan_and_save(scanner: AppScanner, *, path: Path | None = None) -> Catalog:
    windows_dir = os.environ.get("SystemRoot", r"C:\Windows")
    catalog = build_catalog(scanner.scan(), windows_dir=windows_dir)
    save_catalog(catalog, path)
    return catalog


def summary(catalog: Catalog) -> dict[str, int]:
    blocked = sum(1 for a in catalog.apps if a.blocked)
    return {"apps": len(catalog.apps), "blocked": blocked}
