"""Free RAM, free disk and mains power, and the rule for refusing new heavy tasks (step 18).

A heavy task is anything that runs Qwen: every chat request the Router handles (the shared
model is ~3.5 GB loaded, ~4.8 GB peak on this laptop). Light things — approvals, Stop,
reminders, the health page — always work, so the owner can still answer and stop.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import psutil
from pydantic import BaseModel

from backend.reliability.settings import AppSettings

MB = 1024 * 1024
GB = 1024 * MB


class ResourceSnapshot(BaseModel):
    free_ram_mb: int
    total_ram_mb: int
    # The lowest free space among the drives HCWMA writes to (data folder, repository).
    free_disk_gb: float
    disk_path: str
    on_battery: bool | None  # None: no battery (desktop) or unknown
    battery_percent: int | None


def take_snapshot(paths: list[Path]) -> ResourceSnapshot:
    mem = psutil.virtual_memory()
    lowest: tuple[float, str] | None = None
    for path in paths:
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent  # the data folder may not exist yet; its drive does
        try:
            free = shutil.disk_usage(probe).free / GB
        except OSError:
            continue
        if lowest is None or free < lowest[0]:
            lowest = (free, str(probe))
    battery = None
    try:
        battery = psutil.sensors_battery()
    except (OSError, RuntimeError, AttributeError):
        battery = None
    return ResourceSnapshot(
        free_ram_mb=int(mem.available / MB),
        total_ram_mb=int(mem.total / MB),
        free_disk_gb=round(lowest[0], 1) if lowest else 0.0,
        disk_path=lowest[1] if lowest else "",
        on_battery=None if battery is None else not battery.power_plugged,
        battery_percent=None if battery is None else int(battery.percent),
    )


def low_resource_warnings(snap: ResourceSnapshot, settings: AppSettings) -> list[str]:
    """Plain-language warnings; empty when everything is fine."""
    warnings = []
    if snap.free_ram_mb < settings.min_free_ram_mb:
        warnings.append(
            f"Only {snap.free_ram_mb / 1024:.1f} GB of RAM is free (HCWMA needs at least "
            f"{settings.min_free_ram_mb / 1024:.0f} GB). Close some programs."
        )
    if snap.free_disk_gb < settings.min_free_disk_gb:
        warnings.append(
            f"Only {snap.free_disk_gb:.1f} GB of disk is free on {snap.disk_path} (HCWMA needs "
            f"at least {settings.min_free_disk_gb:.0f} GB). Free some space."
        )
    return warnings


def heavy_task_refusal(
    snap: ResourceSnapshot,
    settings: AppSettings,
    *,
    model_paused: bool = False,
    check_resources: bool = True,
) -> str | None:
    """Why a new heavy task must not start now, or None if it may. `check_resources=False`
    (Cloud mode): only the owner's pause counts."""
    if model_paused:
        return (
            'Not started: the model is paused. Choose "Resume model" in the HCWMA tray menu, '
            "then ask again."
        )
    if not check_resources:
        return None
    warnings = low_resource_warnings(snap, settings)
    if warnings:
        return "Not started: the PC is low on resources. " + " ".join(warnings)
    if settings.heavy_only_when_plugged_in and snap.on_battery:
        return (
            'Not started: the laptop is on battery and "Heavy tasks only when plugged in" is '
            "on. Plug in, or turn that off in the HCWMA tray menu."
        )
    return None
