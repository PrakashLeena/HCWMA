"""Windows side of the app inventory (owner decision 2026-11-21). Read-only.

- `Get-StartApps`: every app in the Start menu with its AppUserModelID (Store and desktop).
- `Get-AppxPackage`: Store packages, for their publisher and package family.
- Start Menu shortcuts (all users + this user): the program each one starts. Read through the
  `WScript.Shell` object's `CreateShortcut`, which only reads the .lnk file.
- The installed-programs registry keys (HKLM, HKLM WOW6432Node, HKCU): name, publisher and
  the program from DisplayIcon. System components and updates are skipped.
- The web browsers Windows knows (HKLM and HKCU): `Clients/StartMenuInternet`, every
  `RegisteredApplications` entry whose URLAssociations handle http or https, and this user's
  http/https default. Their programs, names and AppUserModelIDs are hard-blocked.

The PowerShell script is fixed text; nothing from the owner, an agent or a web page is ever put
into it. Nothing is installed, changed or started.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from integrations.apps.blocklist import RegisteredBrowser
from integrations.apps.catalog import InstalledProgram, RawScan, Shortcut, StartApp, StorePackage

CREATE_NO_WINDOW = 0x08000000
_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$start = @(Get-StartApps | ForEach-Object { @{ name = $_.Name; app_id = $_.AppID } })
$pkgs = @(Get-AppxPackage | ForEach-Object { @{ name = $_.Name; publisher = $_.Publisher;
  family = $_.PackageFamilyName; framework = [bool]$_.IsFramework } })
$shell = New-Object -ComObject WScript.Shell
$dirs = @("$env:ProgramData\Microsoft\Windows\Start Menu\Programs",
          "$env:APPDATA\Microsoft\Windows\Start Menu\Programs")
$links = @(foreach ($d in $dirs) {
  Get-ChildItem -LiteralPath $d -Filter *.lnk -Recurse | ForEach-Object {
    @{ name = $_.BaseName; target = $shell.CreateShortcut($_.FullName).TargetPath } } })
@{ start_apps = $start; packages = $pkgs; shortcuts = $links } | ConvertTo-Json -Depth 4 -Compress
"""
_UNINSTALL = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
_UNINSTALL_32 = r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"


def _powershell() -> str:
    root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    return str(root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def parse_script_output(raw: str) -> RawScan:
    data = json.loads(raw) if raw.strip() else {}

    def items(key: str) -> list[dict[str, object]]:
        value = data.get(key) or []
        return [value] if isinstance(value, dict) else [v for v in value if isinstance(v, dict)]

    return RawScan(
        start_apps=[
            StartApp(name=str(i.get("name") or ""), app_id=str(i.get("app_id") or ""))
            for i in items("start_apps")
        ],
        packages=[
            StorePackage(
                name=str(i.get("name") or ""),
                publisher=str(i.get("publisher") or ""),
                family=str(i.get("family") or ""),
                framework=bool(i.get("framework")),
            )
            for i in items("packages")
            if i.get("family")
        ],
        shortcuts=[
            Shortcut(name=str(i.get("name") or ""), target=str(i.get("target") or ""))
            for i in items("shortcuts")
            if i.get("target")
        ],
    )


def _registry_programs() -> list[InstalledProgram]:
    if sys.platform != "win32":
        return []
    import winreg

    out: list[InstalledProgram] = []
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, _UNINSTALL),
        (winreg.HKEY_LOCAL_MACHINE, _UNINSTALL_32),
        (winreg.HKEY_CURRENT_USER, _UNINSTALL),
    ]
    for hive, path in roots:
        try:
            root = winreg.OpenKey(hive, path)
        except OSError:
            continue
        with root:
            count = winreg.QueryInfoKey(root)[0]
            for index in range(count):
                try:
                    with winreg.OpenKey(root, winreg.EnumKey(root, index)) as key:
                        values = {}
                        for name in (
                            "DisplayName", "Publisher", "DisplayIcon", "SystemComponent",
                            "ParentKeyName", "ReleaseType",
                        ):  # fmt: skip
                            try:
                                values[name] = winreg.QueryValueEx(key, name)[0]
                            except OSError:
                                pass
                except OSError:
                    continue
                if not values.get("DisplayName") or values.get("SystemComponent") == 1:
                    continue
                if values.get("ParentKeyName") or values.get("ReleaseType"):
                    continue  # updates and hotfixes, not apps
                out.append(
                    InstalledProgram(
                        name=str(values["DisplayName"]),
                        publisher=str(values.get("Publisher") or ""),
                        exe=os.path.expandvars(str(values.get("DisplayIcon") or "")),
                    )
                )
    return out


_SMI = r"SOFTWARE\Clients\StartMenuInternet"
_REGISTERED = r"SOFTWARE\RegisteredApplications"
_USER_CHOICE = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\{}\UserChoice"


def command_exe(command: str) -> str:
    """The program of a shell command: '"C:\\x\\b.exe" --single-argument %1' -> C:\\x\\b.exe."""
    command = os.path.expandvars(command.strip())
    if command.startswith('"'):
        return command[1:].split('"', 1)[0]
    end = command.lower().find(".exe")
    return command[: end + 4] if end >= 0 else command.split(" ", 1)[0]


def _value(hive: int, path: str, name: str = "") -> str:
    import winreg

    try:
        with winreg.OpenKey(hive, path) as key:
            value = winreg.QueryValueEx(key, name)[0]
    except OSError:
        return ""
    return value if isinstance(value, str) else ""


def _subkeys(hive: int, path: str) -> list[str]:
    import winreg

    try:
        with winreg.OpenKey(hive, path) as key:
            return [winreg.EnumKey(key, i) for i in range(winreg.QueryInfoKey(key)[0])]
    except OSError:
        return []


def _values(hive: int, path: str) -> dict[str, str]:
    import winreg

    try:
        with winreg.OpenKey(hive, path) as key:
            pairs = [winreg.EnumValue(key, i) for i in range(winreg.QueryInfoKey(key)[1])]
    except OSError:
        return {}
    return {str(n).lower(): v for n, v, _ in pairs if isinstance(v, str)}


def _from_progid(progid: str, name: str = "") -> RegisteredBrowser:
    import winreg

    root = winreg.HKEY_CLASSES_ROOT
    return RegisteredBrowser(
        name=name or _value(root, progid + r"\Application", "ApplicationName"),
        exe=command_exe(_value(root, progid + r"\shell\open\command")),
        app_id=_value(root, progid + r"\Application", "AppUserModelID"),
    )


def registered_browsers() -> list[RegisteredBrowser]:
    """Every web browser Windows has registered (both hives), plus this user's http/https
    default. Read-only."""
    if sys.platform != "win32":
        return []
    import winreg

    out: list[RegisteredBrowser] = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for key in _subkeys(hive, _SMI):
            base = rf"{_SMI}\{key}"
            name = _value(hive, base) or _value(hive, base + r"\Capabilities", "ApplicationName")
            out.append(
                RegisteredBrowser(
                    name=name if not name.startswith("@") else key.split(".")[0],
                    exe=command_exe(_value(hive, base + r"\shell\open\command")),
                )
            )
        for caps in _values(hive, _REGISTERED).values():
            urls = _values(hive, caps + r"\URLAssociations")
            progid = urls.get("https") or urls.get("http")
            if progid:
                app_name = _value(hive, caps, "ApplicationName")
                out.append(_from_progid(progid, "" if app_name.startswith("@") else app_name))
    for scheme in ("http", "https"):
        progid = _value(winreg.HKEY_CURRENT_USER, _USER_CHOICE.format(scheme), "ProgId")
        if progid:
            out.append(_from_progid(progid))
    unique = {(b.name, b.exe.casefold(), b.app_id): b for b in out if b.name or b.exe or b.app_id}
    return list(unique.values())


class WindowsAppScanner:
    def scan(self) -> RawScan:
        done = subprocess.run(  # noqa: S603 - fixed program and fixed script text
            [_powershell(), "-NoProfile", "-NonInteractive", "-Command", _SCRIPT],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
        try:
            raw = parse_script_output(done.stdout)
        except ValueError:
            raw = RawScan()
        raw.programs = _registry_programs()
        raw.browsers = registered_browsers()
        return raw
