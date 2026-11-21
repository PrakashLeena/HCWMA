"""Apps HCWMA may never open, whatever the owner's list, the catalog or an approval says.

Web browsers, password managers and authenticators, banking / payment / crypto apps, terminals,
code editors and anything that runs commands, system and security tools, remote-control tools,
app stores and installers, and HCWMA itself. Checked in code on every list, scan and launch;
a blocked app never gets an approval card (owner decision 2026-11-21).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import PureWindowsPath

from integrations.adapters.desktop.ui_interface import (
    BLOCKED_TITLE_WORDS,
    BROWSERS,
    COMMAND_RUNNERS,
    PASSWORD_MANAGERS,
    SYSTEM_SECURITY,
)

# One word (lower case) that marks an off-limits app, on top of the App Automation lists.
_EXTRA_WORDS = frozenset(
    {
        # browsers
        "edge", "browser", "floorp", "operagx",
        # terminals, shells, developer tools that run code
        "terminal", "console", "shell", "command", "prompt", "cmdlet", "powershell", "wsl",
        "ubuntu", "debian", "kali", "linux", "git", "jupyter", "anaconda", "conda", "docker",
        "msys2", "cygwin", "putty", "winscp", "ssh",
        # app stores and installers
        "store", "winget", "installer", "setup", "steam", "uninstall", "uninstaller",
        # system and security tools
        "settings", "registry", "regedit", "msconfig", "defender", "firewall", "security",
        "recovery", "admin", "administrative", "controlpanel", "immersivecontrolpanel",
        "update", "updater", "bios", "diskpart",
        # remote control
        "rdp", "mstsc", "quickassist", "anydesk", "teamviewer", "rustdesk", "vnc",
        # passwords, authenticators, keys
        "authenticator", "keychain", "passwords", "passkey", "protonpass",
        # banking, payments, crypto
        "bank", "banking", "exodus", "metamask", "ledger", "trezor", "electrum",
        # HCWMA itself
        "hcwma",
    }
)  # fmt: skip
OFF_LIMITS_WORDS = BROWSERS | PASSWORD_MANAGERS | COMMAND_RUNNERS | SYSTEM_SECURITY | _EXTRA_WORDS
# Phrases (lower case) looked for anywhere in the name.
OFF_LIMITS_PHRASES = (
    *BLOCKED_TITLE_WORDS,
    "control panel", "device manager", "disk management", "computer management", "event viewer",
    "registry editor", "system information", "system configuration", "resource monitor",
    "performance monitor", "windows tools", "group policy", "remote desktop", "quick assist",
    "file explorer", "visual studio", "android studio", "command prompt", "app installer",
    "epic games", "gog galaxy",
)  # fmt: skip
# Parts of a Store package family / AppUserModelID that mark one.
OFF_LIMITS_ID_PARTS = (
    "windowsstore", "windowsterminal", "powershell", "immersivecontrolpanel", "securityhealth",
    "sechealthui", "desktopappinstaller", "microsoftedge", "windows.defender", "msedge",
    "windows.explorer", "quickassist", "remotedesktop",
)  # fmt: skip

_REASON = "off-limits for HCWMA (browsers, passwords, banking, terminals, system tools, app stores)"


def words(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", text.lower()) if w}


def blocked_reason(
    names: Iterable[str],
    *,
    app_id: str = "",
    exe: str = "",
    processes: Iterable[str] = (),
    windows_dir: str = "",
) -> str | None:
    """Why HCWMA may never open this app, or None. `names`: display name, aliases, publisher.
    `windows_dir`: programs under it (system tools: cmd, regedit, control, explorer…) are
    blocked."""
    texts = [n for n in names if n]
    exe_name = PureWindowsPath(exe).name.lower().removesuffix(".exe") if exe else ""
    procs = {p.lower().removesuffix(".exe") for p in processes} | (
        {exe_name} if exe_name else set()
    )
    found = set().union(*(words(t) for t in [*texts, app_id]), *(words(p) for p in procs), procs)
    hit = sorted(found & OFF_LIMITS_WORDS)
    if hit:
        return f"'{hit[0]}': {_REASON}"
    joined = " ".join(texts).lower()
    for phrase in OFF_LIMITS_PHRASES:
        if phrase in joined:
            return f"'{phrase}': {_REASON}"
    low_id = app_id.lower()
    for part in OFF_LIMITS_ID_PARTS:
        if part in low_id:
            return f"'{part}': {_REASON}"
    if exe and windows_dir:
        try:
            if PureWindowsPath(exe).is_relative_to(PureWindowsPath(windows_dir)):
                return f"a Windows system program: {_REASON}"
        except ValueError:
            pass
    return None
