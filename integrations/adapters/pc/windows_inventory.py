"""Windows side of the app inventory (owner decision 2026-11-21). Read-only.

- `Get-StartApps`: every app in the Start menu with its AppUserModelID (Store and desktop).
- `Get-AppxPackage`: Store packages, for their publisher and package family.
- Start Menu shortcuts (all users + this user): the program each one starts. Read through the
  `WScript.Shell` object's `CreateShortcut`, which only reads the .lnk file.
- The installed-programs registry keys (HKLM, HKLM WOW6432Node, HKCU): name, publisher and
  the program from DisplayIcon. System components and updates are skipped.

The PowerShell script is fixed text; nothing from the owner, an agent or a web page is ever put
into it. Nothing is installed, changed or started.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

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
        return raw
