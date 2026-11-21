"""Apps HCWMA may never open, whatever the owner's list, the catalog or an approval says.

Web browsers (by name, and any program Windows has registered as a web browser), password
managers and authenticators, banking / payment / crypto apps, terminals, code editors, IDEs,
language runtimes, virtual machines and emulators, system / security / backup / update tools,
antivirus and VPN clients, remote-control tools, app stores and installers, the local model
server and HCWMA itself. Checked in code on every list, scan and launch; a blocked app never
gets an approval card, and an earlier "Always allow" does not open it (owner decisions
2026-11-21).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import PureWindowsPath

from pydantic import BaseModel

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
        "edge", "browser", "floorp", "operagx", "comet", "perplexity", "maxthon",
        "seamonkey", "palemoon", "ungoogled", "ladybird", "mullvadbrowser",
        # terminals, shells, developer tools that run code
        "terminal", "console", "shell", "command", "prompt", "cmdlet", "powershell", "wsl",
        "ubuntu", "debian", "kali", "linux", "git", "jupyter", "anaconda", "conda", "docker",
        "msys2", "cygwin", "putty", "winscp", "ssh",
        # IDEs, editors that run code, language runtimes (owner decision 2026-11-21)
        "antigravity", "netbeans", "netbeans64", "devcpp", "vscode", "vscodium", "jetbrains",
        "intellij", "pycharm", "webstorm", "clion", "goland", "phpstorm", "rubymine",
        "datagrip", "rustrover", "eclipse", "codeblocks", "geany", "thonny", "spyder", "rstudio",
        "matlab", "nodejs", "npm", "deno", "pip", "ruby", "perl", "php", "gitbash",
        "kiro", "trae", "zed",
        # coding agents and database clients (owner decision 2026-11-21)
        "chatgpt", "openai", "codex", "githubcopilot", "mongodb", "javacpl",
        # remote consoles
        "rcon", "rcontool",
        # virtual machines and emulators
        "virtualbox", "vbox", "vmware", "qemu", "hyperv", "vmconnect", "bluestacks", "ldplayer",
        "noxplayer", "memu", "genymotion", "emulator", "sandbox", "parallels",
        # app stores and installers
        "store", "winget", "installer", "setup", "steam", "uninstall", "uninstaller",
        # system and security tools
        "settings", "registry", "regedit", "msconfig", "defender", "firewall", "security",
        "recovery", "admin", "administrative", "controlpanel", "immersivecontrolpanel",
        "update", "updater", "bios", "diskpart", "backup", "restore", "powertoys",
        # antivirus and anti-malware
        "malwarebytes", "malware", "antimalware", "antivirus", "virus", "norton", "mcafee",
        "avast", "avg", "kaspersky", "bitdefender", "eset", "sophos", "avira", "webroot",
        "trendmicro", "comodo", "zonealarm", "glasswire", "spybot",
        # VPN and network clients
        "vpn", "cloudflare", "warp", "wireguard", "openvpn", "nordvpn", "expressvpn",
        "protonvpn", "surfshark", "tailscale", "zerotier", "hamachi", "forticlient",
        "globalprotect", "anyconnect", "mullvad", "windscribe", "cyberghost", "tunnelbear",
        "hotspotshield", "psiphon", "softether", "pritunl",
        # the local model server HCWMA runs itself
        "ollama",
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
    "epic games", "gog galaxy", "dev-c++", "git bash", "node.js", "hyper-v", "anti-malware",
    "anti-virus", "windows backup", "check for updates", "1.1.1.1", "android emulator",
    "about java", "configure java", "mysql workbench", "mongodb compass", "github copilot",
)  # fmt: skip
# Parts of a Store package family / AppUserModelID that mark one.
OFF_LIMITS_ID_PARTS = (
    "windowsstore", "windowsterminal", "powershell", "immersivecontrolpanel", "securityhealth",
    "sechealthui", "desktopappinstaller", "microsoftedge", "windows.defender", "msedge",
    "windows.explorer", "quickassist", "remotedesktop", "windowsbackup", "powertoys",
    "electron.app.antigravity", "openai.", "githubcopilot", "javacpl",
)  # fmt: skip

_REASON = "off-limits for HCWMA (browsers, passwords, banking, terminals, system tools, app stores)"


def words(text: str) -> set[str]:
    """Words of `text`, each also without a plural "s" ("Check For Updates" -> update)."""
    found = {w for w in re.split(r"[^a-z0-9]+", text.lower()) if w}
    return found | {w[:-1] for w in found if len(w) > 3 and w.endswith("s")}


def _plain(text: str) -> str:
    return " ".join(w for w in re.split(r"[^a-z0-9]+", text.lower()) if w)


class RegisteredBrowser(BaseModel):
    """A program Windows has registered as a web browser (StartMenuInternet, or a
    RegisteredApplications entry that handles http/https, or the http/https default)."""

    name: str = ""
    exe: str = ""
    app_id: str = ""  # AppUserModelID, when Windows gives one


BROWSER_REASON = "registered as a web browser in Windows: " + _REASON


def browser_match(
    browsers: Iterable[RegisteredBrowser], *, name: str = "", app_id: str = "", exe: str = ""
) -> str | None:
    """BROWSER_REASON when the app is one of the registered browsers (same program, same
    AppUserModelID or same name), else None."""
    want_exe = exe.casefold().strip('"')
    want_id = app_id.casefold().split("!")[0]
    want_name = _plain(name)
    for b in browsers:
        if want_exe and b.exe and want_exe == b.exe.casefold().strip('"'):
            return BROWSER_REASON
        if want_id and b.app_id and want_id == b.app_id.casefold().split("!")[0]:
            return BROWSER_REASON
        if want_name and b.name and not b.name.startswith("@"):
            if want_name == _plain(b.name):
                return BROWSER_REASON
    return None


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
