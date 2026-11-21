"""Bundled adapters, which app each one serves, and whether the owner turned it on
(owner decision 2026-11-21).

An adapter is code that ships WITH HCWMA (in this repository). HCWMA never downloads an adapter
or any other code from the internet; signed adapter packs may come later. Each adapter declares
the app it serves (Store package family, program name, publisher), so after an app scan HCWMA
can offer "WhatsApp found: enable the WhatsApp adapter?". Apps without an adapter are used
through App Automation, which asks before every click and keystroke.

An adapter that is off has its tools refused by the gateway (and hidden from agents). The
on/off state is saved per PC in the HCWMA data folder (`adapters.json`); only owner clicks
(Settings page, first-run wizard) change it, and every change is audited.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from integrations.apps.catalog import Catalog, CatalogApp
from security.audit.audit_logger import AuditLogger, get_audit_logger

ADAPTERS_FILE = "adapters.json"


@dataclass(frozen=True)
class AdapterInfo:
    id: str
    name: str
    tools: tuple[str, ...]
    # Which installed app it serves (any one matches). Empty: it serves no local app (an API).
    package_families: tuple[str, ...] = ()
    exe_names: tuple[str, ...] = ()
    publishers: tuple[str, ...] = ()
    login_hint: str = ""

    @property
    def needs_app(self) -> bool:
        return bool(self.package_families or self.exe_names or self.publishers)

    def serves(self, app: CatalogApp) -> bool:
        if app.blocked:
            return False
        if app.package_family and app.package_family.casefold() in {
            f.casefold() for f in self.package_families
        }:
            return True
        exe = app.process_names()
        if exe and f"{exe[0]}.exe" in {e.casefold() for e in self.exe_names}:
            return True
        return bool(app.publisher) and app.publisher.casefold() in {
            p.casefold() for p in self.publishers
        }


# The adapters that ship with HCWMA. Nothing is ever added from outside this list at run time.
BUNDLED: tuple[AdapterInfo, ...] = (
    AdapterInfo(
        id="whatsapp",
        name="WhatsApp",
        tools=("whatsapp_list_chats", "whatsapp_read_chat", "whatsapp_send"),
        package_families=("5319275A.WhatsAppDesktop_cv1g1gvanyjgm",),
        exe_names=("whatsapp.exe", "whatsapp.root.exe"),
        publishers=("WhatsApp Inc.", "WhatsApp LLC"),
        login_hint="Open WhatsApp and link it to your phone (scan the QR code on your phone).",
    ),
    AdapterInfo(
        id="gmail",
        name="Gmail",
        tools=("search_messages", "read_message", "draft_message", "send_message"),
        login_hint=(
            "Sign in once in a terminal: python -m integrations.adapters.official_api."
            "gmail_login login"
        ),
    ),
)
_BY_TOOL = {tool: a for a in BUNDLED for tool in a.tools}


def adapter(adapter_id: str) -> AdapterInfo | None:
    return next((a for a in BUNDLED if a.id == adapter_id), None)


def adapter_for_tool(tool: str) -> AdapterInfo | None:
    return _BY_TOOL.get(tool)


# ---------- on / off ----------


class AdapterState(BaseModel):
    on: bool
    since: str
    source: str


class AdaptersFile(BaseModel):
    adapters: dict[str, AdapterState] = Field(default_factory=dict)


_lock = threading.Lock()
_cache: dict[Path, tuple[float, AdaptersFile]] = {}


def adapters_path(data_dir: Path | None = None) -> Path:
    from memory.config import resolve_data_dir

    return (data_dir or resolve_data_dir()) / ADAPTERS_FILE


def load_adapters(path: Path | None = None) -> AdaptersFile:
    target = path or adapters_path()
    try:
        mtime = target.stat().st_mtime
    except OSError:
        return AdaptersFile()
    cached = _cache.get(target)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        data = AdaptersFile.model_validate_json(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = AdaptersFile()  # unreadable: everything off (fail closed)
    _cache[target] = (mtime, data)
    return data


def is_on(adapter_id: str, path: Path | None = None) -> bool:
    state = load_adapters(path).adapters.get(adapter_id)
    return bool(state and state.on)


def set_adapter(
    adapter_id: str,
    on: bool,
    *,
    source: str,
    audit: AuditLogger | None = None,
    path: Path | None = None,
) -> None:
    info = adapter(adapter_id)
    if info is None:
        raise ValueError(f"no bundled adapter named '{adapter_id}'")
    target = path or adapters_path()
    with _lock:
        data = load_adapters(target).model_copy(deep=True)
        data.adapters[adapter_id] = AdapterState(
            on=on, since=time.strftime("%Y-%m-%d %H:%M"), source=source
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data.model_dump(), indent=1), encoding="utf-8")
        os.replace(tmp, target)
    (audit or get_audit_logger()).app_access(
        action="adapter_on" if on else "adapter_off", app=info.name, outcome="on" if on else "off",
        reason=source,
    )  # fmt: skip


def adapter_refusal(tool: str, path: Path | None = None) -> str | None:
    """None when `tool` belongs to no adapter or its adapter is on."""
    info = adapter_for_tool(tool)
    if info is None or is_on(info.id, path):
        return None
    return (
        f"the {info.name} adapter is off on this PC; turn it on in HCWMA's Apps page "
        "(Settings) first"
    )


# ---------- after a scan ----------


def matches(catalog: Catalog) -> dict[str, list[CatalogApp]]:
    """adapter id -> the installed apps it serves."""
    return {a.id: [app for app in catalog.apps if a.serves(app)] for a in BUNDLED if a.needs_app}


def offers(catalog: Catalog, path: Path | None = None) -> list[dict[str, str]]:
    """'WhatsApp found: enable the WhatsApp adapter?' for every adapter whose app was found
    and that the owner has not decided on yet."""
    decided = load_adapters(path).adapters
    out: list[dict[str, str]] = []
    for adapter_id, apps in matches(catalog).items():
        info = adapter(adapter_id)
        if info is None or not apps or adapter_id in decided:
            continue
        out.append(
            {
                "adapter": adapter_id,
                "app": apps[0].name,
                "text": f"{apps[0].name} found: enable the {info.name} adapter?",
            }
        )
    return out
