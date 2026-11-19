"""System Settings tools (owner decision 2026-11-19, docs/DECISIONS.md).

A short, fixed list of reversible Windows toggles, through official APIs (the Windows code is
in `windows_settings.py`, behind `SettingsBackend`):

- `get_settings_status` (T1, read only): Wi-Fi, Bluetooth, volume, mute, brightness, dark or
  light mode, battery saver, battery level. Answered by code, without a second model call.
- `set_radio` (Wi-Fi / Bluetooth on or off), `set_volume` (level and/or mute),
  `set_brightness`, `set_theme` (dark / light): T3, the owner approves EVERY call.

Nothing else in Windows settings is reachable: there is no tool for admin-only settings,
firewall, Defender, accounts, updates, drivers, DNS, network configuration or the registry
(except the two per-user theme values `set_theme` writes). Battery saver and Do Not Disturb
have no public Windows API to switch them, so they are not switched at all.

After every change the tool reads the setting back and fails honestly if Windows did not
apply it.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from integrations.mcp.tool_schemas import (
    PermissionTier,
    ToolManifest,
    ToolSpec,
    ToolStatus,
)

# Owner decision 2026-11-19 (DECISIONS.md, "System Settings agent"): the owner set the tools,
# their tiers and the approval rule in the add-agent request. A changed tool here starts as
# "quarantined" again until the owner has seen it.
SETTINGS_TOOLS_STATUS: ToolStatus = "approved"

RadioName = Literal["wifi", "bluetooth"]
Theme = Literal["dark", "light"]
SettingName = Literal["all", "wifi", "bluetooth", "volume", "brightness", "theme", "battery"]
RADIO_LABEL: dict[str, str] = {"wifi": "Wi-Fi", "bluetooth": "Bluetooth"}


class SettingsError(RuntimeError):
    """Windows could not read or change a setting. The message is safe to show."""


class SettingsStatus(BaseModel):
    """None = this PC cannot report it (no such radio, external screen, ...)."""

    wifi: bool | None = None
    bluetooth: bool | None = None
    volume: int | None = None
    muted: bool | None = None
    brightness: int | None = None
    theme: Theme | None = None
    battery_saver: Literal["on", "off", "unavailable"] = "unavailable"
    battery_percent: int | None = None
    on_battery: bool | None = None


class SettingsBackend(Protocol):
    def status(self) -> SettingsStatus: ...
    def set_radio(self, radio: RadioName, on: bool) -> None: ...
    def set_volume(self, level: int | None, mute: bool | None) -> None: ...
    def set_brightness(self, level: int) -> None: ...
    def set_theme(self, dark: bool) -> None: ...


@dataclass(frozen=True)
class SettingsDeps:
    backend: SettingsBackend
    # Reading a setting back right after a change: Windows may need a moment (radios).
    settle_s: float = 3.0
    sleep: Callable[[float], None] = field(default=time.sleep)


def _status_quietly(deps: SettingsDeps) -> SettingsStatus | None:
    """For the approval prompt only: read the current state on a short-lived thread (the
    caller may be inside an event loop or a UI thread), or None if Windows does not answer."""
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(deps.backend.status).result(timeout=10)
    except Exception:  # noqa: BLE001 - the prompt still says what will happen
        return None
    finally:
        pool.shutdown(wait=False)


def _on_off(value: bool | None) -> str:
    return "unknown" if value is None else ("on" if value else "off")


def _read(deps: SettingsDeps) -> SettingsStatus:
    try:
        return deps.backend.status()
    except SettingsError:
        raise
    except Exception as exc:  # noqa: BLE001 - shown to the agent as a plain failure
        raise SettingsError(
            f"Windows could not report the settings ({type(exc).__name__})"
        ) from None


def _apply(
    deps: SettingsDeps, change: Callable[[], None], done: Callable[[SettingsStatus], bool]
) -> SettingsStatus:
    """Run one change, then read back until Windows reports it (or give up honestly)."""
    try:
        change()
    except SettingsError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SettingsError(f"Windows refused the change ({type(exc).__name__}: {exc})") from None
    waited = 0.0
    while True:
        after = _read(deps)
        if done(after) or waited >= deps.settle_s:
            break
        deps.sleep(0.25)
        waited += 0.25
    if not done(after):
        raise SettingsError("Windows did not apply the change; the setting is unchanged")
    return after


# ---------- get_settings_status (T1) ----------


class StatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    setting: SettingName = Field(default="all", description="which setting, or 'all'")


class StatusOutput(BaseModel):
    setting: SettingName
    status: SettingsStatus


def _status(deps: SettingsDeps, args: StatusInput) -> StatusOutput:
    return StatusOutput(setting=args.setting, status=_read(deps))


def _lines(s: SettingsStatus) -> dict[str, str]:
    volume = "unknown" if s.volume is None else f"{s.volume}%"
    if s.muted:
        volume += " (muted)"
    if s.battery_percent is None:
        battery = f"battery saver {s.battery_saver} (no battery found)"
    else:
        source = "on battery" if s.on_battery else "plugged in"
        battery = f"{s.battery_percent}%, {source}, battery saver {s.battery_saver}"
    return {
        "wifi": f"Wi-Fi: {_on_off(s.wifi)}",
        "bluetooth": f"Bluetooth: {_on_off(s.bluetooth)}",
        "volume": f"Volume: {volume}",
        "brightness": "Brightness: " + ("unknown" if s.brightness is None else f"{s.brightness}%"),
        "theme": f"Mode: {s.theme or 'unknown'}",
        "battery": f"Battery: {battery}",
    }


def answer_status(out: StatusOutput) -> str:
    lines = _lines(out.status)
    if out.setting != "all":
        return lines[out.setting] + "."
    return "Current settings:\n" + "\n".join(f"- {line}" for line in lines.values())


# ---------- set_radio (T3) ----------


class SetRadioInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    radio: RadioName
    state: Literal["on", "off"]


class ChangeOutput(BaseModel):
    changed: str  # what was done, in plain words
    status: SettingsStatus


def _set_radio(deps: SettingsDeps, args: SetRadioInput) -> ChangeOutput:
    want = args.state == "on"
    label = RADIO_LABEL[args.radio]
    if getattr(_read(deps), args.radio) is None:
        raise SettingsError(f"this PC has no {label} radio that Windows can switch")
    after = _apply(
        deps,
        lambda: deps.backend.set_radio(args.radio, want),
        lambda s: getattr(s, args.radio) is want,
    )
    return ChangeOutput(changed=f"{label} is now {args.state}", status=after)


def _describe_radio(deps: SettingsDeps, args: SetRadioInput) -> str:
    label = RADIO_LABEL[args.radio]
    s = _status_quietly(deps)
    now = _on_off(getattr(s, args.radio)) if s else "unknown"
    return f"Turn {label} {args.state.upper()} on this PC (it is {now} now)."


# ---------- set_volume (T3) ----------


class SetVolumeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: int | None = Field(default=None, ge=0, le=100, description="volume in percent")
    mute: bool | None = Field(default=None, description="true = mute, false = unmute")

    @model_validator(mode="after")
    def _something(self) -> SetVolumeInput:
        if self.level is None and self.mute is None:
            raise ValueError("give a level, mute, or both")
        return self


def _volume_words(args: SetVolumeInput) -> str:
    parts = []
    if args.level is not None:
        parts.append(f"set the volume to {args.level}%")
    if args.mute is not None:
        parts.append("mute the sound" if args.mute else "unmute the sound")
    return " and ".join(parts)


def _set_volume(deps: SettingsDeps, args: SetVolumeInput) -> ChangeOutput:
    def done(s: SettingsStatus) -> bool:
        level_ok = args.level is None or (s.volume is not None and abs(s.volume - args.level) <= 1)
        return level_ok and (args.mute is None or s.muted is args.mute)

    after = _apply(deps, lambda: deps.backend.set_volume(args.level, args.mute), done)
    words = _volume_words(args)
    return ChangeOutput(changed=words[0].upper() + words[1:] + ": done", status=after)


def _describe_volume(deps: SettingsDeps, args: SetVolumeInput) -> str:
    s = _status_quietly(deps)
    now = "unknown" if s is None else f"{s.volume}%" + (", muted" if s.muted else "")
    words = _volume_words(args)
    return f"{words[0].upper() + words[1:]} on this PC (now: {now})."


# ---------- set_brightness (T3) ----------


class SetBrightnessInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: int = Field(ge=0, le=100, description="screen brightness in percent")


def _set_brightness(deps: SettingsDeps, args: SetBrightnessInput) -> ChangeOutput:
    if _read(deps).brightness is None:
        raise SettingsError("Windows cannot set this screen's brightness (external monitor?)")
    after = _apply(
        deps,
        lambda: deps.backend.set_brightness(args.level),
        lambda s: s.brightness is not None and abs(s.brightness - args.level) <= 2,
    )
    return ChangeOutput(changed=f"Brightness is now {after.brightness}%", status=after)


def _describe_brightness(deps: SettingsDeps, args: SetBrightnessInput) -> str:
    s = _status_quietly(deps)
    now = "unknown" if s is None or s.brightness is None else f"{s.brightness}%"
    return f"Set the screen brightness to {args.level}% (now: {now})."


# ---------- set_theme (T3) ----------


class SetThemeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Theme


def _set_theme(deps: SettingsDeps, args: SetThemeInput) -> ChangeOutput:
    after = _apply(
        deps, lambda: deps.backend.set_theme(args.mode == "dark"), lambda s: s.theme == args.mode
    )
    return ChangeOutput(changed=f"Windows is now in {args.mode} mode", status=after)


def _describe_theme(deps: SettingsDeps, args: SetThemeInput) -> str:
    s = _status_quietly(deps)
    now = (s.theme if s else None) or "unknown"
    return (
        f"Switch Windows and apps to {args.mode.upper()} mode (now: {now}). Changes only the two "
        "per-user theme values; nothing else."
    )


# ---------- specs ----------


def settings_tool_specs(deps: SettingsDeps, status: ToolStatus = "quarantined") -> list[ToolSpec]:
    def manifest(name: str, description: str, change: bool) -> ToolManifest:
        return ToolManifest(
            name=name,
            version="0.1.0",
            created_by="developer",
            status=status,
            route="pc",
            description=description,
            permission_tier=PermissionTier.T3 if change else PermissionTier.T1,
            approval_required=change,
            side_effects="writes" if change else "none",
            timeout_s=20,
        )

    return [
        ToolSpec(
            manifest=manifest(
                "get_settings_status",
                "Read Wi-Fi, Bluetooth, volume, brightness, dark/light mode and battery saver.",
                change=False,
            ),
            input_model=StatusInput,
            output_model=StatusOutput,
            handler=lambda a: _status(deps, a),
            answer=answer_status,
        ),
        ToolSpec(
            manifest=manifest(
                "set_radio", "Turn Wi-Fi or Bluetooth on or off (owner approves).", change=True
            ),
            input_model=SetRadioInput,
            output_model=ChangeOutput,
            handler=lambda a: _set_radio(deps, a),
            describe=lambda a: _describe_radio(deps, a),
        ),
        ToolSpec(
            manifest=manifest(
                "set_volume", "Set the volume in percent and/or mute (owner approves).", change=True
            ),
            input_model=SetVolumeInput,
            output_model=ChangeOutput,
            handler=lambda a: _set_volume(deps, a),
            describe=lambda a: _describe_volume(deps, a),
        ),
        ToolSpec(
            manifest=manifest(
                "set_brightness",
                "Set the screen brightness in percent (owner approves).",
                change=True,
            ),
            input_model=SetBrightnessInput,
            output_model=ChangeOutput,
            handler=lambda a: _set_brightness(deps, a),
            describe=lambda a: _describe_brightness(deps, a),
        ),
        ToolSpec(
            manifest=manifest(
                "set_theme", "Switch Windows to dark or light mode (owner approves).", change=True
            ),
            input_model=SetThemeInput,
            output_model=ChangeOutput,
            handler=lambda a: _set_theme(deps, a),
            describe=lambda a: _describe_theme(deps, a),
        ),
    ]


class UnsupportedSettings:
    """Non-Windows PCs: every call fails honestly."""

    def _no(self) -> SettingsError:
        return SettingsError("system settings are only supported on Windows for now")

    def status(self) -> SettingsStatus:
        raise self._no()

    def set_radio(self, radio: RadioName, on: bool) -> None:
        raise self._no()

    def set_volume(self, level: int | None, mute: bool | None) -> None:
        raise self._no()

    def set_brightness(self, level: int) -> None:
        raise self._no()

    def set_theme(self, dark: bool) -> None:
        raise self._no()


def default_settings_tool_specs() -> list[ToolSpec]:
    if sys.platform == "win32":
        from integrations.adapters.pc.windows_settings import WindowsSettings

        backend: SettingsBackend = WindowsSettings()
    else:
        backend = UnsupportedSettings()
    return settings_tool_specs(SettingsDeps(backend), status=SETTINGS_TOOLS_STATUS)
