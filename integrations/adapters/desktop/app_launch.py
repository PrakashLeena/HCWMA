"""Open an installed app from the owner's allowlist (owner decision 2026-11-20, docs/DECISIONS.md).

    launch_app  T2  open ONE app on the owner's list in `configs/apps.yaml`, by its installed
                    Store / Start-menu entry. Nothing is downloaded or installed.

The WhatsApp tools use the same launcher when WhatsApp Desktop is not running, so "List my
WhatsApp chats" works with WhatsApp closed.

Rules enforced here, in code:

- Only apps on the owner's list can be opened. Each entry is pinned to its package (the start of
  its AppUserModelID, which includes the publisher), so a look-alike shortcut named "WhatsApp"
  is never started.
- Off-limits apps can never be on the list, whatever the YAML says: web browsers, password
  managers, terminals / code editors / anything that runs commands, system security and
  settings, banking and payment apps, app stores and installers, and HCWMA itself. Such an
  entry is dropped when the list is read.
- The tool opens only an app the owner named in their own request (text an agent read cannot
  name one).
- One launch at a time; one attempt per request (a failed app is not tried again for a minute),
  never a retry loop. A failure gives a plain message.
- Opening an app never lets App Automation click or type in it: `UiGuard` still decides that.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from integrations import activity
from integrations.adapters.desktop.ui_interface import (
    BLOCKED_TITLE_WORDS,
    BROWSERS,
    COMMAND_RUNNERS,
    PASSWORD_MANAGERS,
    SYSTEM_SECURITY,
)
from integrations.mcp.tool_schemas import (
    CallerContext,
    PermissionTier,
    ToolManifest,
    ToolSpec,
    ToolStatus,
)
from security.permissions.config import REPO_ROOT

CONFIG_PATH = REPO_ROOT / "configs" / "apps.yaml"
# The owner asked for launch_app on 2026-11-20 (docs/DECISIONS.md).
LAUNCH_TOOL_STATUS: ToolStatus = "approved"
WAIT_S = 30.0
MAX_WAIT_S = 90.0
FAILED_COOLDOWN_S = 60.0

# ---------- what can never be on the list ----------

# Names (one word, lower case) that mark an off-limits app, on top of the App Automation lists.
_EXTRA_OFF_LIMITS = frozenset(
    {
        "edge", "browser", "terminal", "console", "shell", "command", "prompt", "cmdlet",
        "store", "winget", "installer", "setup", "settings", "authenticator", "keychain",
        "hcwma", "registry", "bank", "banking",
    }
)  # fmt: skip
OFF_LIMITS_WORDS = (
    BROWSERS | PASSWORD_MANAGERS | COMMAND_RUNNERS | SYSTEM_SECURITY | _EXTRA_OFF_LIMITS
)


def _words(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", text.lower()) if w}


def off_limits_reason(entry: AppEntry) -> str | None:
    """Why this app may never be opened by HCWMA, or None."""
    texts = [entry.id, entry.name, entry.app_id_prefix, *entry.aliases]
    names = {p.lower().removesuffix(".exe") for p in entry.processes}
    hit = sorted(
        (set().union(*(_words(t) for t in texts)) | names | _words(" ".join(names)))
        & OFF_LIMITS_WORDS
    )
    if hit:
        return (
            f"'{hit[0]}' marks an off-limits app (browsers, passwords, terminals, system, banking)"
        )
    joined = " ".join(texts).lower()
    for word in BLOCKED_TITLE_WORDS:
        if word in joined:
            return f"'{word}' marks an off-limits app (banking, payments, passwords)"
    return None


# ---------- the allowlist ----------

_APP_ID = re.compile(r"^[A-Za-z0-9._-]{3,120}![A-Za-z0-9._-]{1,60}$")


class AppEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    name: str = Field(min_length=2, max_length=40)
    aliases: tuple[str, ...] = ()
    # The start of the app's AppUserModelID: "<package name>_<publisher id>", e.g.
    # "5319275A.WhatsAppDesktop_cv1g1gvanyjgm". Only an installed app whose id starts with it.
    app_id_prefix: str = Field(pattern=r"^[A-Za-z0-9._-]{3,120}$")
    processes: tuple[str, ...] = Field(min_length=1)  # how a running copy is recognised
    # How long to wait for it to be ready after opening it (one attempt, then a message).
    wait_s: float = Field(default=WAIT_S, ge=5, le=MAX_WAIT_S)

    @field_validator("processes")
    @classmethod
    def _lower(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(p.lower().removesuffix(".exe") for p in value)

    def names(self) -> list[str]:
        return [self.id.replace("_", " "), self.name, *self.aliases]


class AppsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apps: list[AppEntry] = Field(default_factory=list)


@dataclass(frozen=True)
class Allowlist:
    apps: dict[str, AppEntry]
    rejected: dict[str, str] = field(default_factory=dict)  # id -> why it was dropped

    def find(self, name: str) -> AppEntry | None:
        wanted = _norm(name)
        for entry in self.apps.values():
            if wanted in {_norm(n) for n in entry.names()}:
                return entry
        return None

    def display(self) -> str:
        return ", ".join(e.name for e in self.apps.values()) or "none"


def _norm(text: str) -> str:
    return " ".join(re.split(r"[^a-z0-9]+", text.casefold())).strip()


def build_allowlist(entries: list[AppEntry]) -> Allowlist:
    apps: dict[str, AppEntry] = {}
    rejected: dict[str, str] = {}
    for entry in entries:
        reason = off_limits_reason(entry)
        if reason:
            rejected[entry.id] = reason
        elif entry.id not in apps:
            apps[entry.id] = entry
    return Allowlist(apps, rejected)


def load_allowlist(path: Path = CONFIG_PATH) -> Allowlist:
    if not path.exists():
        return Allowlist({})
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return build_allowlist(AppsConfig.model_validate(raw).apps)


# ---------- the launcher ----------


class AppStarter(Protocol):
    """What an OS adapter provides. It does NOT decide what may be opened."""

    def find_app_id(self, prefix: str) -> str | None:
        """The installed app's AppUserModelID starting with `prefix`, or None."""
        ...

    def start(self, app_id: str) -> None: ...

    def is_running(self, processes: frozenset[str]) -> bool: ...


class LaunchRefused(PermissionError):
    """The app is not on the owner's list (or is off-limits). Nothing was started."""


class LaunchFailed(RuntimeError):
    """The app could not be opened. The message is safe to show the owner."""


@dataclass(frozen=True)
class LaunchResult:
    app: str
    launched: bool  # False: it was already running


class AppLauncher:
    def __init__(
        self,
        starter: AppStarter,
        allowlist: Allowlist | None = None,
        *,
        wait_s: float | None = None,  # None: each app's own `wait_s` from the list
        cooldown_s: float = FAILED_COOLDOWN_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._starter = starter
        self._list = allowlist if allowlist is not None else load_allowlist()
        self._wait = wait_s
        self._cooldown = cooldown_s
        self._clock = clock
        self._sleep = sleep
        self._busy = threading.Lock()
        self._failed: dict[str, float] = {}  # app id -> when the last attempt failed

    @property
    def allowlist(self) -> Allowlist:
        return self._list

    def entry(self, name: str) -> AppEntry:
        entry = self._list.find(name)
        if entry is None:
            raise LaunchRefused(
                f"'{name}' is not on your list of apps HCWMA may open ({self._list.display()}); "
                "the owner adds apps in configs/apps.yaml"
            )
        return entry

    def is_running(self, name: str) -> bool:
        return self._starter.is_running(frozenset(self.entry(name).processes))

    def ensure_running(
        self,
        name: str,
        *,
        ready: Callable[[], bool] | None = None,
        start_even_if_running: bool = False,
    ) -> LaunchResult:
        """Open the app unless it is running, then wait (up to `wait_s`) until it is running and
        `ready()` says True. `ready` may raise to stop the wait with its own message. One
        attempt only: nothing here retries. `start_even_if_running`: the caller saw no window
        (an app closed to the background keeps its process); starting it again shows it."""
        entry = self.entry(name)
        processes = frozenset(entry.processes)
        if not start_even_if_running and self._starter.is_running(processes):
            return LaunchResult(entry.name, launched=False)
        if not self._busy.acquire(blocking=False):
            raise LaunchFailed("HCWMA is already opening an app; try again in a moment")
        try:
            last = self._failed.get(entry.id)
            if last is not None and self._clock() - last < self._cooldown:
                raise LaunchFailed(
                    f"HCWMA could not open {entry.name} a moment ago and does not keep trying; "
                    f"open {entry.name} yourself, then ask again"
                )
            try:
                self._launch_and_wait(entry, processes, ready)
            except BaseException:
                self._failed[entry.id] = self._clock()
                raise
            self._failed.pop(entry.id, None)
            return LaunchResult(entry.name, launched=True)
        finally:
            self._busy.release()

    def _launch_and_wait(
        self, entry: AppEntry, processes: frozenset[str], ready: Callable[[], bool] | None
    ) -> None:
        try:
            app_id = self._starter.find_app_id(entry.app_id_prefix)
        except OSError as exc:
            raise LaunchFailed(f"HCWMA could not look up {entry.name}: {exc}") from None
        if app_id is None:
            raise LaunchFailed(
                f"{entry.name} is not installed on this PC (HCWMA never downloads or installs apps)"
            )
        if not _APP_ID.match(app_id) or not app_id.startswith(entry.app_id_prefix):
            raise LaunchFailed(f"{entry.name}'s installed entry looks wrong; nothing was opened")
        with activity.notice(f"Opening {entry.name}…"):
            try:
                self._starter.start(app_id)
            except OSError:
                raise LaunchFailed(f"Windows could not start {entry.name}") from None
            wait = self._wait if self._wait is not None else entry.wait_s
            deadline = self._clock() + wait
            while True:
                if self._starter.is_running(processes) and (ready is None or ready()):
                    return
                if self._clock() >= deadline:
                    raise LaunchFailed(
                        f"{entry.name} did not finish opening within {int(wait)} s; "
                        f"check {entry.name} on your screen and ask again"
                    )
                self._sleep(0.5)


# ---------- the tool ----------


class LaunchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app: str = Field(
        min_length=2,
        max_length=40,
        description="The app's name as the owner typed it, e.g. Calculator, Notepad, WhatsApp.",
    )


class LaunchOutput(BaseModel):
    app: str
    opened: bool
    already_running: bool
    note: str


def named_by_owner(entry: AppEntry, owner_words: str) -> bool:
    said = f" {_norm(owner_words)} "
    return any(f" {_norm(n)} " in said for n in entry.names() if _norm(n))


def _precheck(launcher: AppLauncher) -> Callable[[LaunchInput, CallerContext | None], str | None]:
    def check(args: LaunchInput, caller: CallerContext | None) -> str | None:
        try:
            entry = launcher.entry(args.app)
        except LaunchRefused as exc:
            return str(exc)
        if caller is None or not named_by_owner(entry, caller.owner_words):
            return (
                f"you did not ask to open {entry.name} in this request; HCWMA only opens apps "
                "you name"
            )
        return None

    return check


def _launch(launcher: AppLauncher, args: LaunchInput, caller: CallerContext) -> LaunchOutput:
    entry = launcher.entry(args.app)
    if not named_by_owner(entry, caller.owner_words):  # defence in depth: precheck did this
        raise LaunchRefused(f"you did not ask to open {entry.name}")
    result = launcher.ensure_running(entry.id)
    note = f"{result.app} is open now." if result.launched else f"{result.app} was already open."
    return LaunchOutput(
        app=result.app, opened=result.launched, already_running=not result.launched, note=note
    )


def launch_tool_spec(launcher: AppLauncher, status: ToolStatus = "quarantined") -> ToolSpec:
    names = launcher.allowlist.display()
    return ToolSpec(
        manifest=ToolManifest(
            name="launch_app",
            version="0.1.0",
            created_by="developer",
            status=status,
            route="desktop",
            description=(
                f"Open ONE installed app the owner named, only from the owner's list ({names}). "
                "Waits until it is open. Never installs anything; other apps are refused."
            )[:500],
            permission_tier=PermissionTier.T2,
            approval_required=False,
            side_effects="writes",  # starts a program (reversible: the owner closes it)
            timeout_s=MAX_WAIT_S + 15,
        ),
        input_model=LaunchInput,
        output_model=LaunchOutput,
        handler=lambda a, c: _launch(launcher, a, c),
        caller_context=True,
        precheck=_precheck(launcher),
    )


_default: AppLauncher | None = None
_default_lock = threading.Lock()


def default_launcher() -> AppLauncher | None:
    """The one launcher of this process (Windows only for now), shared by `launch_app` and the
    WhatsApp tools so "one launch at a time" holds across them."""
    global _default  # noqa: PLW0603
    import sys

    if sys.platform != "win32":
        return None
    with _default_lock:
        if _default is None:
            from integrations.adapters.pc.windows_apps import WindowsAppStarter

            _default = AppLauncher(WindowsAppStarter())
        return _default


def default_launch_tool_specs() -> list[ToolSpec]:
    launcher = default_launcher()
    return [] if launcher is None else [launch_tool_spec(launcher, status=LAUNCH_TOOL_STATUS)]
