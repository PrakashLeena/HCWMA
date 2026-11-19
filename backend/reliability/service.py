"""Everything step 18 adds to the running backend, in one object.

- `recover()` at start: what a crash left running is marked interrupted, never re-run.
- `run(stop_control)`: while the app runs — a full backup whenever the newest is a day old
  (checked hourly) and the sleep watch (PC woke up -> interrupt what was running).
- `refusal()`: why a new heavy task must not start (paused, low RAM/disk, on battery).
- `health()`: the health page.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Protocol

import anyio

from backend.reliability.backup import BackupManager
from backend.reliability.health import (
    CachedProbe,
    HealthItem,
    HealthReport,
    Probe,
    gmail_probe,
    run_probes,
    whatsapp_probe,
)
from backend.reliability.journal import RecoveryReport, TaskJournal, journal_path
from backend.reliability.model_control import ModelControl, OllamaModelControl
from backend.reliability.resources import (
    ResourceSnapshot,
    heavy_task_refusal,
    low_resource_warnings,
    take_snapshot,
)
from backend.reliability.settings import AppSettings, SettingsStore
from backend.reliability.sleep_watch import watch_for_sleep
from memory.config import REPO_ROOT, resolve_data_dir
from security.audit.audit_logger import AuditLogger, get_audit_logger

log = logging.getLogger(__name__)

BACKUP_CHECK_S = 3600.0


class Interruptible(Protocol):
    """`agents.runtime.stop.StopControl`."""

    def interrupt_all(self, reason: str) -> object: ...


class ReliabilityService:
    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        audit: AuditLogger | None = None,
        backups: BackupManager | None = None,
        model: ModelControl | None = None,
        probes: dict[str, Probe] | None = None,
        snapshot: Callable[[], ResourceSnapshot] | None = None,
        autostart_enabled: Callable[[], bool | None] | None = None,
        auto_backup: bool = True,
    ) -> None:
        self.data_dir = data_dir or resolve_data_dir()
        self.audit = audit or get_audit_logger()
        self.journal = TaskJournal(journal_path(self.data_dir), audit=self.audit)
        self.settings = SettingsStore(self.data_dir)
        self.backups = backups or BackupManager(self.data_dir)
        self.model = model or OllamaModelControl()
        self._snapshot = snapshot or (lambda: take_snapshot([self.data_dir, REPO_ROOT]))
        self._autostart = autostart_enabled or _autostart_enabled
        self._probes = probes or {
            "Gmail login": CachedProbe(gmail_probe, ttl_s=600),
            "WhatsApp linked": CachedProbe(whatsapp_probe, ttl_s=60),
        }
        self.auto_backup = auto_backup
        self.recovery = RecoveryReport()
        self.show_requested = False  # a second start asked the running app to show itself

    # ---------- start ----------

    def recover(self) -> RecoveryReport:
        self.recovery = self.journal.recover()
        if self.recovery.interrupted_tasks or self.recovery.cancelled_approvals:
            log.warning("recovered after an unclean exit: %s", self.recovery.message())
        return self.recovery

    # ---------- while running ----------

    @asynccontextmanager
    async def run(self, stop: Callable[[], Interruptible | None]) -> AsyncIterator[None]:
        async def on_wake(seconds: float) -> None:
            control = stop()
            if control is None:
                return
            minutes = max(1, round(seconds / 60))
            control.interrupt_all(f"the PC was asleep for about {minutes} minute(s)")

        async def backup_loop() -> None:
            while True:
                try:
                    if self.backups.due():
                        await anyio.to_thread.run_sync(self.backup_now, "daily")
                except Exception:  # noqa: BLE001 - try again next hour; never stop the app
                    log.exception("daily backup failed")
                await anyio.sleep(BACKUP_CHECK_S)

        async with anyio.create_task_group() as tg:
            if self.auto_backup:
                tg.start_soon(backup_loop)
            tg.start_soon(watch_for_sleep, on_wake)
            try:
                yield
            finally:
                tg.cancel_scope.cancel()

    def backup_now(self, reason: str = "owner") -> Path:
        folder = self.backups.backup(label=reason)
        self.audit.reliability(
            action="backup", outcome="written", reason=f"{reason}: {folder.name}"
        )
        return folder

    def current_settings(self) -> AppSettings:
        return self.settings.load()

    def update_settings(self, **changes: object) -> AppSettings:
        settings = self.settings.update(**changes)
        self.audit.reliability(action="settings", outcome="changed", reason=str(changes))
        return settings

    def snapshot(self) -> ResourceSnapshot:
        return self._snapshot()

    def refusal(self) -> str | None:
        return heavy_task_refusal(
            self.snapshot(), self.settings.load(), model_paused=self.model.paused
        )

    def pause_model(self) -> None:
        self.model.pause()
        self.audit.reliability(action="pause_model", outcome="paused")

    def resume_model(self) -> None:
        self.model.resume()
        self.audit.reliability(action="resume_model", outcome="resumed")

    # ---------- health page ----------

    def _model_items(self) -> list[HealthItem]:
        try:
            status = self.model.status()
        except Exception:  # noqa: BLE001 - Ollama not running
            return [
                HealthItem(name="Ollama", ok=False, value="not running (start Ollama)"),
                HealthItem(name="Model loaded", ok=None, value="unknown: Ollama is not running"),
            ]
        if status["loaded"]:
            loaded = HealthItem(
                name="Model loaded", ok=True, value=f"yes ({status['size_mb'] / 1024:.1f} GB)"
            )
        elif self.model.paused:
            loaded = HealthItem(name="Model loaded", ok=None, value="no: paused by you")
        else:
            loaded = HealthItem(
                name="Model loaded", ok=None, value="no (loads on the next request)"
            )
        return [HealthItem(name="Ollama", ok=True, value=f"running ({status['version']})"), loaded]

    def health(self) -> HealthReport:
        settings = self.settings.load()
        found = run_probes(self._probes)
        snap = self.snapshot()
        items = self._model_items()
        ram_ok = snap.free_ram_mb >= settings.min_free_ram_mb
        disk_ok = snap.free_disk_gb >= settings.min_free_disk_gb
        items.append(
            HealthItem(
                name="RAM free",
                ok=ram_ok,
                value=f"{snap.free_ram_mb / 1024:.1f} GB of {snap.total_ram_mb / 1024:.0f} GB",
            )
        )
        items.append(
            HealthItem(
                name="Disk free", ok=disk_ok, value=f"{snap.free_disk_gb:.1f} GB ({snap.disk_path})"
            )
        )
        if snap.on_battery is None:
            power = HealthItem(name="Power", ok=None, value="no battery found")
        else:
            source = "on battery" if snap.on_battery else "plugged in"
            power = HealthItem(name="Power", ok=True, value=f"{source}, {snap.battery_percent}%")
        items.append(power)
        items.extend(found.values())
        newest = self.backups.newest_time()
        if newest is None:
            items.append(HealthItem(name="Last backup", ok=False, value="none yet"))
        else:
            age_h = (datetime.now() - newest).total_seconds() / 3600
            items.append(
                HealthItem(
                    name="Last backup",
                    ok=age_h < 48,
                    value=f"{newest:%a %d %b %H:%M} ({len(self.backups.backups())} kept)",
                )
            )
        return HealthReport(
            items=items,
            warnings=low_resource_warnings(snap, settings),
            heavy_tasks_refused=heavy_task_refusal(snap, settings, model_paused=self.model.paused),
            model_paused=self.model.paused,
            settings=settings.model_dump(),
            start_with_windows=self._autostart(),
            recovery=self.recovery.message(),
            checked_at=datetime.now().isoformat(timespec="seconds"),
        )


def _autostart_enabled() -> bool | None:
    try:
        from app.desktop.autostart import get_autostart

        return get_autostart().is_enabled()
    except Exception:  # noqa: BLE001
        return None
