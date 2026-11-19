# ruff: noqa: E501  (COM vtable declarations read best one method per line)
"""Windows-only code for the System Settings agent (owner decision 2026-11-19), behind
`SettingsBackend` in `system_settings.py`. Official Windows APIs only, no UI clicking and no
admin rights:

- Wi-Fi / Bluetooth: WinRT `Windows.Devices.Radios.Radio` (the same switch as Quick Settings;
  it never touches network configuration, adapters or drivers).
- Volume / mute: Core Audio `IAudioEndpointVolume` on the default playback device (comtypes).
- Brightness: WMI `root\\wmi` `WmiMonitorBrightness(Methods)` (built-in laptop screen).
- Dark / light mode: the ONE registry exception the owner allowed: the two per-user values
  `AppsUseLightTheme` and `SystemUsesLightTheme` under `HKCU\\...\\Themes\\Personalize`, then a
  settings-change broadcast. Key and value names are constants; nothing else is ever written.
- Battery saver: read only (WinRT `PowerManager`). Windows has no public API to switch it.
"""

from __future__ import annotations

import asyncio
import ctypes
import winreg
from collections.abc import Callable, Coroutine, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from ctypes import HRESULT, POINTER, c_float, c_uint, c_void_p
from ctypes.wintypes import BOOL, DWORD, LPCWSTR
from typing import Any, TypeVar

import comtypes
import comtypes.client
from comtypes import COMMETHOD, GUID, IUnknown

from integrations.adapters.pc.system_settings import RadioName, SettingsError, SettingsStatus

T = TypeVar("T")

# ---------- Core Audio (only the methods used; the vtable order must match) ----------

CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
_E_RENDER, _E_MULTIMEDIA, _CLSCTX_ALL = 0, 1, 23


class IAudioEndpointVolume(IUnknown):
    _iid_ = GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")
    _methods_ = [
        COMMETHOD([], HRESULT, "RegisterControlChangeNotify", (["in"], c_void_p, "p")),
        COMMETHOD([], HRESULT, "UnregisterControlChangeNotify", (["in"], c_void_p, "p")),
        COMMETHOD([], HRESULT, "GetChannelCount", (["out"], POINTER(c_uint), "n")),
        COMMETHOD([], HRESULT, "SetMasterVolumeLevel", (["in"], c_float, "db"), (["in"], c_void_p, "ctx")),
        COMMETHOD([], HRESULT, "SetMasterVolumeLevelScalar", (["in"], c_float, "level"), (["in"], c_void_p, "ctx")),
        COMMETHOD([], HRESULT, "GetMasterVolumeLevel", (["out"], POINTER(c_float), "db")),
        COMMETHOD([], HRESULT, "GetMasterVolumeLevelScalar", (["out"], POINTER(c_float), "level")),
        COMMETHOD([], HRESULT, "SetChannelVolumeLevel", (["in"], c_uint, "c"), (["in"], c_float, "db"), (["in"], c_void_p, "ctx")),
        COMMETHOD([], HRESULT, "SetChannelVolumeLevelScalar", (["in"], c_uint, "c"), (["in"], c_float, "l"), (["in"], c_void_p, "ctx")),
        COMMETHOD([], HRESULT, "GetChannelVolumeLevel", (["in"], c_uint, "c"), (["out"], POINTER(c_float), "db")),
        COMMETHOD([], HRESULT, "GetChannelVolumeLevelScalar", (["in"], c_uint, "c"), (["out"], POINTER(c_float), "l")),
        COMMETHOD([], HRESULT, "SetMute", (["in"], BOOL, "mute"), (["in"], c_void_p, "ctx")),
        COMMETHOD([], HRESULT, "GetMute", (["out"], POINTER(BOOL), "mute")),
    ]  # fmt: skip


class IMMDevice(IUnknown):
    _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
    _methods_ = [
        COMMETHOD(
            [], HRESULT, "Activate",
            (["in"], POINTER(GUID), "iid"), (["in"], DWORD, "ctx"),
            (["in"], c_void_p, "params"), (["out"], POINTER(POINTER(IUnknown)), "out"),
        ),
    ]  # fmt: skip


class IMMDeviceEnumerator(IUnknown):
    _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    _methods_ = [
        COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], DWORD, "flow"), (["in"], DWORD, "mask"), (["out"], POINTER(c_void_p), "out")),
        COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (["in"], DWORD, "flow"), (["in"], DWORD, "role"), (["out"], POINTER(POINTER(IMMDevice)), "out")),
    ]  # fmt: skip


# ---------- theme: the only registry values ever written ----------

_THEME_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
_THEME_VALUES = ("AppsUseLightTheme", "SystemUsesLightTheme")
_HWND_BROADCAST, _WM_SETTINGCHANGE, _SMTO_ABORTIFHUNG = 0xFFFF, 0x001A, 0x0002


def _run(coro: Coroutine[Any, Any, T]) -> T:
    """Run one WinRT call to the end, also when the caller is already inside an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result(timeout=30)


@contextmanager
def _com() -> Iterator[None]:
    """COM for one call on whatever thread the tool server uses."""
    comtypes.CoInitialize()
    try:
        yield
    finally:
        comtypes.CoUninitialize()


def _guarded(read: Callable[[], T]) -> T | None:
    try:
        return read()
    except Exception:  # noqa: BLE001 - one unreadable part must not hide the others
        return None


class WindowsSettings:
    # ---------- radios ----------

    @staticmethod
    def _radios() -> dict[RadioName, Any]:
        from winrt.windows.devices.radios import Radio, RadioAccessStatus, RadioKind

        async def find() -> dict[RadioName, Any]:
            if await Radio.request_access_async() != RadioAccessStatus.ALLOWED:
                raise SettingsError(
                    "Windows does not let apps switch radios (Settings > Privacy > Radios)"
                )
            found: dict[RadioName, Any] = {}
            for radio in await Radio.get_radios_async():
                if radio.kind == RadioKind.WI_FI:
                    found.setdefault("wifi", radio)
                elif radio.kind == RadioKind.BLUETOOTH:
                    found.setdefault("bluetooth", radio)
            return found

        return _run(find())

    @staticmethod
    def _radio_on(radio: Any) -> bool | None:
        from winrt.windows.devices.radios import RadioState

        if radio.state == RadioState.ON:
            return True
        if radio.state == RadioState.OFF:
            return False
        return None  # disabled in the device manager / unknown

    def set_radio(self, radio: RadioName, on: bool) -> None:
        from winrt.windows.devices.radios import RadioAccessStatus, RadioState

        found = self._radios().get(radio)
        if found is None:
            raise SettingsError(f"this PC has no {radio} radio")

        async def switch() -> Any:
            return await found.set_state_async(RadioState.ON if on else RadioState.OFF)

        status = _run(switch())
        if status != RadioAccessStatus.ALLOWED:
            raise SettingsError(f"Windows refused to switch {radio} ({status.name.lower()})")

    # ---------- volume ----------

    @staticmethod
    def _endpoint() -> Any:
        enumerator = comtypes.client.CreateObject(
            CLSID_MMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER, interface=IMMDeviceEnumerator
        )
        device = enumerator.GetDefaultAudioEndpoint(_E_RENDER, _E_MULTIMEDIA)
        unknown = device.Activate(IAudioEndpointVolume._iid_, _CLSCTX_ALL, None)
        return unknown.QueryInterface(IAudioEndpointVolume)

    def _volume(self) -> tuple[int, bool]:
        with _com():
            endpoint = self._endpoint()
            return round(endpoint.GetMasterVolumeLevelScalar() * 100), bool(endpoint.GetMute())

    def set_volume(self, level: int | None, mute: bool | None) -> None:
        with _com():
            endpoint = self._endpoint()
            if level is not None:
                endpoint.SetMasterVolumeLevelScalar(max(0, min(100, level)) / 100, None)
            if mute is not None:
                endpoint.SetMute(mute, None)

    # ---------- brightness ----------

    @staticmethod
    def _wmi_first(cls: str) -> Any:
        wmi = comtypes.client.CoGetObject("winmgmts:{impersonationLevel=impersonate}!root\\wmi")
        for item in wmi.InstancesOf(cls):
            return item
        raise SettingsError("this screen's brightness cannot be set by Windows (external monitor?)")

    def _brightness(self) -> int:
        with _com():
            item = self._wmi_first("WmiMonitorBrightness")
            return int(item.Properties_("CurrentBrightness").Value)

    def set_brightness(self, level: int) -> None:
        with _com():
            item = self._wmi_first("WmiMonitorBrightnessMethods")
            method = item.Methods_("WmiSetBrightness")
            params = method.InParameters.SpawnInstance_()
            params.Properties_("Timeout").Value = 0
            params.Properties_("Brightness").Value = max(0, min(100, level))
            item.ExecMethod_("WmiSetBrightness", params)

    # ---------- theme ----------

    @staticmethod
    def _theme() -> str:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _THEME_KEY) as key:
            light, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return "light" if light else "dark"

    def set_theme(self, dark: bool) -> None:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _THEME_KEY, 0, winreg.KEY_SET_VALUE) as key:
            for name in _THEME_VALUES:
                winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, 0 if dark else 1)
        send = ctypes.windll.user32.SendMessageTimeoutW
        send.argtypes = [c_void_p, c_uint, c_void_p, LPCWSTR, c_uint, c_uint, c_void_p]
        send(
            _HWND_BROADCAST,
            _WM_SETTINGCHANGE,
            0,
            "ImmersiveColorSet",
            _SMTO_ABORTIFHUNG,
            2000,
            None,
        )

    # ---------- battery ----------

    @staticmethod
    def _battery() -> tuple[str, int | None, bool | None]:
        from winrt.windows.system.power import (
            BatteryStatus,
            EnergySaverStatus,
            PowerManager,
            PowerSupplyStatus,
        )

        saver = PowerManager.energy_saver_status
        saver_text = {
            EnergySaverStatus.ON: "on",
            EnergySaverStatus.OFF: "off",
            EnergySaverStatus.DISABLED: "off",  # plugged in: saver cannot be on
        }.get(saver, "unavailable")
        if PowerManager.battery_status == BatteryStatus.NOT_PRESENT:
            return saver_text, None, None
        on_battery = PowerManager.power_supply_status == PowerSupplyStatus.NOT_PRESENT
        return saver_text, int(PowerManager.remaining_charge_percent), on_battery

    # ---------- status ----------

    def status(self) -> SettingsStatus:
        radios = _guarded(self._radios) or {}
        volume = _guarded(self._volume)
        battery = _guarded(self._battery)
        return SettingsStatus(
            wifi=self._radio_on(radios["wifi"]) if "wifi" in radios else None,
            bluetooth=self._radio_on(radios["bluetooth"]) if "bluetooth" in radios else None,
            volume=volume[0] if volume else None,
            muted=volume[1] if volume else None,
            brightness=_guarded(self._brightness),
            theme=_guarded(self._theme),  # type: ignore[arg-type]
            battery_saver=battery[0] if battery else "unavailable",
            battery_percent=battery[1] if battery else None,
            on_battery=battery[2] if battery else None,
        )
