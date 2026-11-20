"""Windows side of `launch_app` (owner decision 2026-11-20): find an installed app's
AppUserModelID in the Start menu list (`Get-StartApps`) and open it through the shell's
AppsFolder, exactly as clicking it in the Start menu would. Nothing is downloaded or installed.

What may be opened is decided in `integrations/adapters/desktop/app_launch.py` before anything
here is called.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import psutil

CREATE_NO_WINDOW = 0x08000000
# Fixed text: no owner or model input ever goes into a command line here.
_GET_START_APPS = "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"


def _windows_dir() -> Path:
    return Path(os.environ.get("SystemRoot", r"C:\Windows"))


def _system32(exe: str) -> str:
    return str(_windows_dir() / "System32" / exe)


def parse_start_apps(raw: str) -> list[tuple[str, str]]:
    """(name, AppUserModelID) pairs from `Get-StartApps | ConvertTo-Json`."""
    if not raw.strip():
        return []
    data = json.loads(raw)
    if isinstance(data, dict):
        data = [data]
    out: list[tuple[str, str]] = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("AppID"), str):
            out.append((str(item.get("Name") or ""), item["AppID"]))
    return out


def pick_app_id(apps: list[tuple[str, str]], prefix: str) -> str | None:
    """The Store app whose id is `<prefix>!<entry>` (the package and publisher must match)."""
    for _name, app_id in apps:
        package, sep, _entry = app_id.partition("!")
        if sep and package.casefold() == prefix.casefold():
            return app_id
    for _name, app_id in apps:  # prefix given without the publisher hash
        package, sep, _entry = app_id.partition("!")
        if sep and package.casefold().startswith(prefix.casefold() + "_"):
            return app_id
    return None


DISABLE_ENV = "HCWMA_DISABLE_APP_LAUNCH"  # set by the test suite: never open a real app


def _check_enabled() -> None:
    if os.environ.get(DISABLE_ENV):
        raise OSError(f"opening apps is turned off ({DISABLE_ENV})")


class WindowsAppStarter:
    def find_app_id(self, prefix: str) -> str | None:
        _check_enabled()
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, fixed script text
                [
                    _system32(r"WindowsPowerShell\v1.0\powershell.exe"),
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    _GET_START_APPS,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                creationflags=CREATE_NO_WINDOW,
                check=False,
            )
            apps = parse_start_apps(done.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        found = pick_app_id(apps, prefix)
        # `startswith(prefix)` is what app_launch checks; return the id with its own casing
        # normalised to the configured prefix so that check holds for case-only differences.
        if found is None:
            return None
        return (
            prefix + found[len(prefix) :]
            if found.casefold().startswith(prefix.casefold())
            else found
        )

    def start(self, app_id: str) -> None:
        _check_enabled()
        # Same as a Start-menu click. explorer.exe returns at once (often with exit code 1),
        # so success is judged by the app's process appearing, not by the exit code.
        subprocess.Popen(  # noqa: S603 - fixed program; app_id checked against the allowlist
            [str(_windows_dir() / "explorer.exe"), f"shell:AppsFolder\\{app_id}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )

    def is_running(self, processes: frozenset[str]) -> bool:
        for proc in psutil.process_iter(["name"]):
            try:
                name = (proc.info.get("name") or "").lower().removesuffix(".exe")
            except psutil.Error:
                continue
            if name in processes:
                return True
        return False
