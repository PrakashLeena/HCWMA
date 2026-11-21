"""Apps, adapters and first-run setup for the backend (owner decision 2026-11-21).

- Scans the installed apps at start (in the background) and then once a day; the owner can also
  scan from the Apps page or the wizard. One scan at a time.
- Gives the Apps page and the wizard what they show, and carries out the owner's clicks there:
  always allow / revoke an app, turn an adapter on / off, add a detected folder, finish setup.
  These are reached only through token-protected routes; no agent tool or voice route calls
  them. Every change is audited (in the modules that make it).
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from integrations.apps import access, adapters, setup_state
from integrations.apps.catalog import AppScanner, Catalog, load_catalog, scan_and_save
from security.audit.audit_logger import get_audit_logger

log = logging.getLogger("hcwma.apps")
DAY_S = 24 * 3600.0


def default_scanner() -> AppScanner | None:
    if sys.platform != "win32":
        return None
    from integrations.adapters.pc.windows_inventory import WindowsAppScanner

    return WindowsAppScanner()


class AppsService:
    def __init__(
        self,
        scanner: AppScanner | None = None,
        *,
        every_s: float = DAY_S,
        overrides: Callable[[], list[str]] | None = None,
    ) -> None:
        self._scanner = scanner
        self._every = every_s
        self._scan_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error = ""
        self._overrides = overrides or _override_names

    # ---------- background ----------

    def start(self) -> None:
        """At backend start: keep the adapters of an existing install on (once), then scan now
        and every `every_s` in the background."""
        try:
            turned_on = setup_state.keep_adapters_of_existing_install()
            if turned_on:
                log.info("adapters kept on for an existing install: %s", ", ".join(turned_on))
        except OSError:
            log.warning("could not write the adapter state")
        if self._scanner is None:
            return
        self._thread = threading.Thread(target=self._loop, name="app-scan", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        why = "start"
        while not self._stop.is_set():
            self.scan(why)
            why = "daily"
            if self._stop.wait(self._every):
                return

    # ---------- owner actions ----------

    def scan(self, why: str = "owner") -> dict[str, Any]:
        if self._scanner is None:
            return {"ok": False, "message": "app scanning works on Windows only"}
        if not self._scan_lock.acquire(blocking=False):
            return {"ok": False, "message": "a scan is already running"}
        try:
            start = time.monotonic()
            try:
                catalog = scan_and_save(self._scanner)
            except Exception as exc:  # noqa: BLE001 - a failed scan keeps the old catalog
                self.last_error = type(exc).__name__
                get_audit_logger().app_access(
                    action="scan", app="-", outcome="failed", reason=f"{why}: {self.last_error}"
                )
                return {"ok": False, "message": f"the scan failed ({self.last_error})"}
            self.last_error = ""
            blocked = sum(1 for a in catalog.apps if a.blocked)
            get_audit_logger().app_access(
                action="scan", app="-", outcome="ok",
                reason=f"{why}: {len(catalog.apps)} apps, {blocked} blocked, "
                f"{time.monotonic() - start:.1f} s",
            )  # fmt: skip
            return {"ok": True, "apps": len(catalog.apps), "blocked": blocked}
        finally:
            self._scan_lock.release()

    def allow(self, app_id: str, source: str) -> str:
        app = load_catalog().get(app_id)
        if app is None:
            raise KeyError("no such app in the catalog")
        if app.blocked:
            raise PermissionError(f"HCWMA never opens {app.name}: {app.blocked}")
        access.allow_always(app.id, app.name, source=source)
        return app.name

    def revoke_app(self, app_id: str) -> bool:
        return access.revoke_always(app_id)

    def set_adapter(self, adapter_id: str, on: bool, source: str) -> None:
        adapters.set_adapter(adapter_id, on, source=source)

    # ---------- what the pages show ----------

    def state(self) -> dict[str, Any]:
        catalog = load_catalog()
        allowed = access.load_access().allowed
        served = {app.id: a.name for a in adapters.BUNDLED for app in catalog.apps if a.serves(app)}
        return {
            "scanned_at": catalog.scanned_at,
            "scanning": self._scan_lock.locked(),
            "last_error": self.last_error,
            "apps": [
                {
                    "id": a.id,
                    "name": a.name,
                    "kind": a.kind,
                    "publisher": a.publisher,
                    "blocked": a.blocked,
                    "allowed": a.id in allowed,
                    "adapter": served.get(a.id, ""),
                }
                for a in catalog.apps
            ],
            "allowed": [v.model_dump() for v in allowed.values()],
            "owner_listed": self._overrides(),
            "adapters": self.adapters_state(catalog),
            "offers": adapters.offers(catalog),
        }

    def adapters_state(self, catalog: Catalog | None = None) -> list[dict[str, Any]]:
        catalog = catalog or load_catalog()
        saved = adapters.load_adapters().adapters
        found = adapters.matches(catalog)
        out = []
        for info in adapters.BUNDLED:
            state = saved.get(info.id)
            out.append(
                {
                    "id": info.id,
                    "name": info.name,
                    "state": "unset" if state is None else ("on" if state.on else "off"),
                    "needs_app": info.needs_app,
                    "found": [a.name for a in found.get(info.id, [])],
                    "login_hint": info.login_hint,
                    "tools": list(info.tools),
                }
            )
        return out


def _override_names() -> list[str]:
    from integrations.adapters.desktop.app_launch import load_allowlist

    try:
        return [e.name for e in load_allowlist().apps.values()]
    except (OSError, ValueError):
        return []
