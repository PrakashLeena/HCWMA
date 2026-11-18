"""Shared interface and safety rules for desktop app automation (build step 12).

The OS-specific part (`integrations/adapters/pc/<os>.py`) only knows how to list windows, read
their accessibility tree and send one click / one text / one key. Everything that decides
WHAT may be touched lives here, in plain Python, so it is the same on every OS and is tested
without a screen:

- `UiGuard.check_window`: the windows that are never touched, whatever the model asks for
  (HCWMA's own windows including the approval window, browsers, password managers, banking
  and payment apps, anything that can run commands, system security and settings windows).
- `is_sensitive_field`: password, PIN, card, one-time-code fields are never read, clicked or
  typed into.
- `looks_like_instructions`: window text is data (rule 13); text that gives orders is flagged.

The owner can ADD to the block lists in `configs/app_automation.yaml`; the built-in lists
below cannot be shortened from there.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Literal, Protocol

import psutil
import yaml
from pydantic import BaseModel, ConfigDict, Field

from security.permissions.config import REPO_ROOT

CONFIG_PATH = REPO_ROOT / "configs" / "app_automation.yaml"

# ---------- what is never touched ----------

# Executable names, lower case, without ".exe".
BROWSERS = frozenset(
    {
        "chrome", "msedge", "msedgewebview2", "firefox", "brave", "opera", "opera_gx", "vivaldi",
        "iexplore", "arc", "tor", "waterfox", "chromium", "librewolf", "yandex", "safari",
        "duckduckgo", "thorium",
    }
)  # fmt: skip
PASSWORD_MANAGERS = frozenset(
    {
        "bitwarden", "1password", "keepass", "keepassxc", "lastpass", "dashlane", "nordpass",
        "enpass", "roboform", "keeper", "credwiz", "credentialuibroker",
    }
)  # fmt: skip
# Anything that can run commands or edit/launch programs: typing into it would run code that
# the model, not the owner, chose (rule 1: the model never executes anything).
COMMAND_RUNNERS = frozenset(
    {
        "cmd", "powershell", "powershell_ise", "pwsh", "windowsterminal", "wt", "conhost",
        "openconsole", "bash", "wsl", "wslhost", "mintty", "python", "pythonw", "py", "node",
        "code", "cursor", "windsurf", "devenv", "pycharm64", "idea64", "webstorm64", "claude",
        "regedit", "mmc", "taskmgr", "wscript", "cscript", "mshta",
    }
)  # fmt: skip
SYSTEM_SECURITY = frozenset(
    {
        "consent", "logonui", "winlogon", "lockapp", "securityhealthsystray", "msascui",
        "windowsdefender", "systemsettings", "control", "startmenuexperiencehost", "searchhost",
        "shellexperiencehost", "sihost", "taskhostw", "netplwiz", "lusrmgr", "secpol",
        "gpedit", "wf", "mpcmdrun",
    }
)  # fmt: skip
BLOCKED_CLASSES = frozenset(
    {"shell_traywnd", "shell_secondarytraywnd", "progman", "workerw", "#32768"}
)  # the taskbar, the desktop, context menus
# Words that mark a banking / payment / saved-password window in its title (fail closed).
BLOCKED_TITLE_WORDS = (
    "hcwma", "password", "passkey", "credential", "keychain", "vault", "bank", "paypal",
    "venmo", "revolut", "stripe", "coinbase", "binance", "kraken", "paytm", "phonepe", "gpay",
    "google pay", "wallet", "brokerage", "zerodha", "robinhood", "credit card", "debit card",
    "payment", "checkout", "windows security", "user account control", "task manager",
)  # fmt: skip
BLOCKED_TITLES_EXACT = frozenset({"run", "settings", "start", "search"})

_SENSITIVE_FIELD = re.compile(
    r"pass(word|code|phrase|key)|\bpin\b|\bcvv\b|\bcvc\b|card\s*(number|no)|security\s*code"
    r"|one[- ]?time|\botp\b|verification\s*code|2fa|secret|api[- ]?key|\btoken\b|\bssn\b"
    r"|account\s*(number|no)|routing\s*number|\biban\b|credit|debit",
    re.IGNORECASE,
)
_TEXT_FIELD_TYPES = frozenset({"edit", "document", "combobox"})  # fields, not labels

ControlTypeName = str


def is_sensitive_field(name: str, control_type: str, is_password: bool = False) -> bool:
    """Password, PIN, card and one-time-code fields. A field flagged as a password by the OS is
    always sensitive; a text box whose label says it is one is treated the same."""
    if is_password:
        return True
    return control_type.lower() in _TEXT_FIELD_TYPES and bool(_SENSITIVE_FIELD.search(name))


class GuardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extra_blocked_apps: list[str] = Field(default_factory=list)
    extra_blocked_title_words: list[str] = Field(default_factory=list)


def load_guard_config(path: Path = CONFIG_PATH) -> GuardConfig:
    if not path.exists():
        return GuardConfig()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return GuardConfig.model_validate(raw)


# ---------- data shapes ----------


class WindowInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    window_id: str  # opaque handle: pass it back exactly
    title: str
    app: str  # executable name, lower case, no ".exe"
    pid: int
    class_name: str = ""


class UiElement(BaseModel):
    model_config = ConfigDict(frozen=True)

    element_id: str  # opaque handle: pass it back exactly
    window_id: str
    name: str
    control_type: ControlTypeName
    enabled: bool = True
    is_password: bool = False


class TextRead(BaseModel):
    text: str
    truncated: bool = False
    sensitive_fields_skipped: int = 0


class UiError(RuntimeError):
    """The action cannot be done as asked. The message is safe to show to the agent."""


class UiBackend(Protocol):
    """What an OS adapter must provide. It does NOT decide what is allowed: it is only ever
    called after `UiGuard` has accepted the window and the element."""

    def list_windows(self) -> list[WindowInfo]: ...
    def get_window(self, window_id: str) -> WindowInfo | None: ...
    def read_text(self, window_id: str, max_chars: int) -> TextRead: ...
    def find_elements(
        self, window_id: str, name_contains: str, control_type: str | None, limit: int
    ) -> list[UiElement]: ...
    def get_element(self, element_id: str) -> UiElement | None: ...
    def click(self, element_id: str) -> str: ...
    def type_text(self, element_id: str, text: str) -> None: ...
    def press_key(self, window_id: str, key: str) -> None: ...


# ---------- the guard ----------


def own_process_ids() -> frozenset[int]:
    """This process and its ancestors (the backend / desktop app that owns the approval window),
    plus the web-view helper processes of those. The Windows shell (explorer.exe) is usually an
    ancestor too, but its windows are the owner's ordinary folders, so it is not counted."""
    ids = {os.getpid()}
    try:
        for proc in psutil.Process().parents():
            name = proc.name().lower().removesuffix(".exe")
            if name in ("explorer", "svchost", "services", "wininit", "winlogon"):
                continue
            ids.add(proc.pid)
            if name.startswith("python") or name == "hcwma":
                try:
                    ids.update(
                        c.pid
                        for c in proc.children(recursive=True)
                        if c.name().lower().startswith("qtwebengineprocess")
                    )
                except psutil.Error:
                    continue
    except psutil.Error:
        pass
    return frozenset(ids)


class UiGuard:
    def __init__(
        self,
        config: GuardConfig | None = None,
        own_pids: Iterable[int] | None = None,
    ) -> None:
        cfg = config if config is not None else load_guard_config()
        self._apps = (BROWSERS | PASSWORD_MANAGERS | COMMAND_RUNNERS | SYSTEM_SECURITY) | {
            a.lower().removesuffix(".exe") for a in cfg.extra_blocked_apps
        }
        self._words = (*BLOCKED_TITLE_WORDS, *(w.lower() for w in cfg.extra_blocked_title_words))
        self._own = None if own_pids is None else frozenset(own_pids)

    def refusal(self, window: WindowInfo) -> str | None:
        """Why this window must not be touched, or None when it may be."""
        own = self._own if self._own is not None else own_process_ids()
        if window.pid in own or window.app.lower().startswith("hcwma"):
            return "this is one of HCWMA's own windows (including its approval window)"
        app = window.app.lower().removesuffix(".exe")
        if app in BROWSERS:
            return "web browsers are off-limits (saved passwords, logins, banking)"
        if app in PASSWORD_MANAGERS:
            return "password managers and credential windows are off-limits"
        if app in COMMAND_RUNNERS:
            return "terminals, code editors and other programs that run commands are off-limits"
        if app in SYSTEM_SECURITY or window.class_name.lower() in BLOCKED_CLASSES:
            return "system security, settings and shell windows are off-limits"
        if app in self._apps:
            return "the owner added this app to the off-limits list"
        title = window.title.lower().strip()
        if title in BLOCKED_TITLES_EXACT:
            return "system dialogs are off-limits"
        for word in self._words:
            if word in title:
                return f"the window title mentions '{word}' (banking, payments, passwords)"
        return None

    def check_window(self, window: WindowInfo) -> None:
        reason = self.refusal(window)
        if reason:
            raise UiError(f"HCWMA may not touch this window: {reason}")


# ---------- window text is data ----------


def looks_like_instructions(text: str) -> bool:
    """Reuses the memory guard's instruction detector (rule 13)."""
    from memory.guard import is_instruction

    return any(is_instruction(line) for line in text.splitlines() if len(line) > 12)


KeyName = Literal[
    "enter", "tab", "shift+tab", "escape", "backspace", "delete", "space", "up", "down", "left",
    "right", "home", "end", "pageup", "pagedown", "ctrl+a", "ctrl+c", "ctrl+z", "ctrl+y",
    "ctrl+f", "ctrl+s",
]  # fmt: skip
