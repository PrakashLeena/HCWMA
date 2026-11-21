"""Open an installed app (owner decisions 2026-11-20 and 2026-11-21, docs/DECISIONS.md).

    launch_app  T2  open ONE installed app the owner named. Any app in this PC's app catalog
                    (`integrations/apps/catalog.py`) can be opened; nothing is downloaded or
                    installed.

Who decides, in code:

- Blocked apps (`integrations/apps/blocklist.py`: browsers, password managers, banking and
  payment, terminals and code runners, system tools, remote control, app stores and installers,
  HCWMA) are refused before anything else. They never get an approval card.
- An app the owner has not allowed yet shows an approval card "HCWMA wants to open <app>" with
  Allow once / Always allow / Deny. Only an owner click answers it (never voice, never an
  agent). "Always allow" is saved per app (`integrations/apps/access.py`) and covers opening
  only: clicks, typing and sends inside the app still ask every time.
- Apps in `configs/apps.yaml` are the owner's overrides: written by the owner, they open without
  a card, and each is pinned to its package + publisher so a look-alike is never started.
- The tool opens only an app the owner named in their own request (text an agent read cannot
  name one). One launch at a time; one attempt (a failed app is not tried again for a minute);
  every failure gives a plain message.

The WhatsApp tools use the same launcher (through the `whatsapp` override) when WhatsApp
Desktop is not running.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from integrations import activity
from integrations.apps.access import allow_always, is_always_allowed
from integrations.apps.blocklist import blocked_reason
from integrations.apps.catalog import Catalog, CatalogApp, load_catalog, norm
from integrations.mcp.tool_schemas import (
    CallerContext,
    PermissionTier,
    ToolManifest,
    ToolSpec,
    ToolStatus,
)
from security.permissions.config import REPO_ROOT

CONFIG_PATH = REPO_ROOT / "configs" / "apps.yaml"
# The owner asked for launch_app on 2026-11-20 and for catalog apps on 2026-11-21.
LAUNCH_TOOL_STATUS: ToolStatus = "approved"
WAIT_S = 30.0
MAX_WAIT_S = 90.0
FAILED_COOLDOWN_S = 60.0


def _windows_dir() -> str:
    return os.environ.get("SystemRoot", r"C:\Windows")


# ---------- the owner's overrides (configs/apps.yaml) ----------


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


def off_limits_reason(entry: AppEntry) -> str | None:
    """Why this override may never be opened by HCWMA, or None."""
    return blocked_reason(
        [entry.id, entry.name, *entry.aliases], app_id=entry.app_id_prefix,
        processes=entry.processes,
    )  # fmt: skip


class AppsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apps: list[AppEntry] = Field(default_factory=list)


@dataclass(frozen=True)
class Allowlist:
    """The owner's overrides that passed the block list."""

    apps: dict[str, AppEntry]
    rejected: dict[str, str] = field(default_factory=dict)  # id -> why it was dropped

    def find(self, name: str) -> AppEntry | None:
        wanted = norm(name)
        for entry in self.apps.values():
            if wanted in {norm(n) for n in entry.names()}:
                return entry
        return None


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


# ---------- what gets started ----------


@dataclass(frozen=True)
class LaunchTarget:
    id: str  # catalog id, or "apps.yaml:<id>" for an owner override
    name: str
    app_id: str = ""  # AppUserModelID (opened through the shell's AppsFolder)
    exe: str = ""  # program path, when there is no AppUserModelID
    processes: tuple[str, ...] = ()
    wait_s: float = WAIT_S
    owner_listed: bool = False  # in configs/apps.yaml: opens without a card
    app_id_prefix: str = ""  # overrides: the installed id must start with this
    aliases: tuple[str, ...] = ()

    def names(self) -> list[str]:
        return [self.name, *self.aliases]


class AppStarter(Protocol):
    """What an OS adapter provides. It does NOT decide what may be opened."""

    def find_app_id(self, prefix: str) -> str | None:
        """The installed app's AppUserModelID starting with `prefix`, or None."""
        ...

    def start(self, app_id: str) -> None: ...

    def start_exe(self, exe: str) -> None: ...

    def is_running(self, processes: frozenset[str]) -> bool: ...

    def window_titles(self) -> dict[int, str]:
        """Visible top-level windows: handle -> title."""
        ...


class LaunchRefused(PermissionError):
    """The app cannot be opened by HCWMA (blocked, unknown, ambiguous). Nothing was started."""


class LaunchFailed(RuntimeError):
    """The app could not be opened. The message is safe to show the owner."""


@dataclass(frozen=True)
class LaunchResult:
    app: str
    launched: bool  # False: it was already running


_SAFE_APP_ID = re.compile(r"^[\w.\-{}\\: !+()&,']{2,260}$")


def _check_app_id(app_id: str) -> bool:
    return bool(_SAFE_APP_ID.match(app_id)) and ".." not in app_id


def _title_matches(name: str, title: str) -> bool:
    short = re.sub(r"^(microsoft|windows)\s+", "", name.strip(), flags=re.IGNORECASE)
    have = f" {norm(title)} "
    return any(f" {norm(n)} " in have for n in {name, short} if norm(n))


class AppLauncher:
    def __init__(
        self,
        starter: AppStarter,
        allowlist: Allowlist | None = None,
        *,
        catalog: Callable[[], Catalog] = load_catalog,
        wait_s: float | None = None,  # None: each app's own wait
        cooldown_s: float = FAILED_COOLDOWN_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._starter = starter
        self._list = allowlist if allowlist is not None else load_allowlist()
        self._catalog = catalog
        self._wait = wait_s
        self._cooldown = cooldown_s
        self._clock = clock
        self._sleep = sleep
        self._busy = threading.Lock()
        self._failed: dict[str, float] = {}  # target id -> when the last attempt failed

    @property
    def allowlist(self) -> Allowlist:
        return self._list

    # ----- which app -----

    def _from_override(self, entry: AppEntry, catalog: Catalog) -> LaunchTarget:
        found = next(
            (
                a
                for a in catalog.apps
                if a.app_id.casefold().startswith(entry.app_id_prefix.casefold() + "!")
            ),
            None,
        )
        return LaunchTarget(
            id=f"apps.yaml:{entry.id}",
            name=entry.name,
            app_id=found.app_id if found else "",
            processes=entry.processes,
            wait_s=entry.wait_s,
            owner_listed=True,
            app_id_prefix=entry.app_id_prefix,
            aliases=tuple(entry.names()),
        )

    @staticmethod
    def _from_catalog(app: CatalogApp) -> LaunchTarget:
        return LaunchTarget(
            id=app.id, name=app.name, app_id=app.app_id, exe=app.exe, processes=app.process_names()
        )

    def target(self, name: str) -> LaunchTarget:
        """The one app `name` means, or LaunchRefused saying why not (blocked / unknown /
        more than one)."""
        catalog = self._catalog()
        entry = self._list.find(name)
        if entry is not None:
            return self._from_override(entry, catalog)
        found = catalog.find(name)
        if not found:
            from_list = self._list.rejected.get(norm(name).replace(" ", "_"))
            if from_list:
                raise LaunchRefused(f"HCWMA never opens '{name}': {from_list}")
            raise LaunchRefused(
                f"'{name}' is not an app HCWMA found on this PC (the Apps page can scan again)"
            )
        allowed = [a for a in found if not a.blocked]
        if not allowed:
            raise LaunchRefused(f"HCWMA never opens {found[0].name}: {found[0].blocked}")
        if len(allowed) > 1:
            names = ", ".join(sorted({a.name for a in allowed})[:6])
            raise LaunchRefused(
                f"more than one app matches '{name}' ({names}); say the app's full name"
            )
        app = allowed[0]
        reason = blocked_reason([app.name, app.publisher], app_id=app.app_id, exe=app.exe,
                                windows_dir=_windows_dir())  # fmt: skip
        if reason:  # the catalog file is data: check again
            raise LaunchRefused(f"HCWMA never opens {app.name}: {reason}")
        return self._from_catalog(app)

    # ----- running? -----

    def _running(self, target: LaunchTarget) -> bool:
        if target.processes:
            return self._starter.is_running(frozenset(target.processes))
        return any(_title_matches(target.name, t) for t in self._starter.window_titles().values())

    def is_running(self, name: str) -> bool:
        return self._running(self.target(name))

    # ----- open -----

    def ensure_running(
        self,
        name: str | LaunchTarget,
        *,
        ready: Callable[[], bool] | None = None,
        start_even_if_running: bool = False,
    ) -> LaunchResult:
        """Open the app unless it is running, then wait (up to its wait time) until it is up
        and `ready()` says True. `ready` may raise to stop the wait with its own message. One
        attempt only: nothing here retries. `start_even_if_running`: the caller saw no window
        (an app closed to the background keeps its process); starting it again shows it."""
        target = name if isinstance(name, LaunchTarget) else self.target(name)
        if not start_even_if_running and self._running(target):
            return LaunchResult(target.name, launched=False)
        if not self._busy.acquire(blocking=False):
            raise LaunchFailed("HCWMA is already opening an app; try again in a moment")
        try:
            last = self._failed.get(target.id)
            if last is not None and self._clock() - last < self._cooldown:
                raise LaunchFailed(
                    f"HCWMA could not open {target.name} a moment ago and does not keep trying; "
                    f"open {target.name} yourself, then ask again"
                )
            try:
                self._launch_and_wait(target, ready)
            except BaseException:
                self._failed[target.id] = self._clock()
                raise
            self._failed.pop(target.id, None)
            return LaunchResult(target.name, launched=True)
        finally:
            self._busy.release()

    def _resolve_app_id(self, target: LaunchTarget) -> str:
        app_id = target.app_id
        if not app_id and target.app_id_prefix:
            try:
                app_id = self._starter.find_app_id(target.app_id_prefix) or ""
            except OSError as exc:
                raise LaunchFailed(f"HCWMA could not look up {target.name}: {exc}") from None
        if target.app_id_prefix and app_id:
            ok = app_id.casefold().startswith(target.app_id_prefix.casefold() + "!")
            if not ok:
                raise LaunchFailed(f"{target.name}'s installed entry looks wrong; nothing opened")
        return app_id

    def _launch_and_wait(self, target: LaunchTarget, ready: Callable[[], bool] | None) -> None:
        app_id = self._resolve_app_id(target)
        exe = "" if app_id else target.exe
        if not app_id and not exe:
            raise LaunchFailed(
                f"{target.name} is not installed on this PC (HCWMA never downloads or installs "
                "apps)"
            )
        if app_id and not _check_app_id(app_id):
            raise LaunchFailed(f"{target.name}'s installed entry looks wrong; nothing was opened")
        if exe and (PureWindowsPath(exe).suffix.lower() != ".exe" or not Path(exe).is_file()):
            raise LaunchFailed(f"{target.name}'s program was not found at its installed place")
        before = set(self._starter.window_titles())
        with activity.notice(f"Opening {target.name}…"):
            try:
                if app_id:
                    self._starter.start(app_id)
                else:
                    self._starter.start_exe(exe)
            except OSError:
                raise LaunchFailed(f"Windows could not start {target.name}") from None
            wait = self._wait if self._wait is not None else target.wait_s
            deadline = self._clock() + wait
            while True:
                if self._up(target, before) and (ready is None or ready()):
                    return
                if self._clock() >= deadline:
                    raise LaunchFailed(
                        f"{target.name} did not finish opening within {int(wait)} s; "
                        f"check {target.name} on your screen and ask again"
                    )
                self._sleep(0.5)

    def _up(self, target: LaunchTarget, before: set[int]) -> bool:
        if target.processes:
            return self._starter.is_running(frozenset(target.processes))
        titles = self._starter.window_titles()
        if any(_title_matches(target.name, t) for t in titles.values()):
            return True
        return any(h not in before and t.strip() for h, t in titles.items())


# ---------- the tool ----------


class LaunchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app: str = Field(
        min_length=2,
        max_length=60,
        description="The app's name as the owner typed it, e.g. Calculator, Spotify, Paint.",
    )


class LaunchOutput(BaseModel):
    app: str
    opened: bool
    already_running: bool
    note: str


def named_by_owner(app: str, target: LaunchTarget, owner_words: str) -> bool:
    """The owner typed this app's name (or the name the agent passed) in their request."""
    said = f" {norm(owner_words)} "
    return any(f" {norm(n)} " in said for n in [app, *target.names()] if norm(n))


def _precheck(launcher: AppLauncher) -> Callable[[LaunchInput, CallerContext | None], str | None]:
    def check(args: LaunchInput, caller: CallerContext | None) -> str | None:
        try:
            target = launcher.target(args.app)
        except LaunchRefused as exc:
            return str(exc)
        if caller is None or not named_by_owner(args.app, target, caller.owner_words):
            return (
                f"you did not ask to open {target.name} in this request; HCWMA only opens apps "
                "you name"
            )
        return None

    return check


def _needs_owner(launcher: AppLauncher) -> Callable[[LaunchInput], bool]:
    def needs(args: LaunchInput) -> bool:
        target = launcher.target(args.app)  # a refusal here fails closed in the gateway
        return not (target.owner_listed or is_always_allowed(target.id))

    return needs


def _remember(launcher: AppLauncher) -> Callable[[LaunchInput], None]:
    def remember(args: LaunchInput) -> None:
        target = launcher.target(args.app)
        allow_always(target.id, target.name, source="approval card")

    return remember


def _describe(launcher: AppLauncher) -> Callable[[LaunchInput], str]:
    def describe(args: LaunchInput) -> str:
        target = launcher.target(args.app)
        how = f"AppUserModelID {target.app_id}" if target.app_id else f"program {target.exe}"
        return (
            f"HCWMA wants to open {target.name}.\n"
            f"Installed app: {how}\n"
            "Allow once: open it now only. Always allow: HCWMA may open it without asking "
            "(opening only; clicks, typing and sends inside it still ask you each time). You "
            "can revoke this on the Apps page."
        )

    return describe


def _launch(launcher: AppLauncher, args: LaunchInput, caller: CallerContext) -> LaunchOutput:
    target = launcher.target(args.app)
    if not named_by_owner(args.app, target, caller.owner_words):  # defence in depth
        raise LaunchRefused(f"you did not ask to open {target.name}")
    result = launcher.ensure_running(target)
    note = f"{result.app} is open now." if result.launched else f"{result.app} was already open."
    return LaunchOutput(
        app=result.app, opened=result.launched, already_running=not result.launched, note=note
    )


def launch_tool_spec(launcher: AppLauncher, status: ToolStatus = "quarantined") -> ToolSpec:
    return ToolSpec(
        manifest=ToolManifest(
            name="launch_app",
            version="0.2.0",
            created_by="developer",
            status=status,
            route="desktop",
            description=(
                "Open ONE installed app the owner named (any app found on this PC). The owner "
                "is asked first unless they always allow that app. Browsers, password managers, "
                "banking, terminals, system tools and app stores are refused. Never installs."
            ),
            permission_tier=PermissionTier.T2,
            approval_required=False,  # asked per app: see ask_owner
            side_effects="writes",  # starts a program (reversible: the owner closes it)
            timeout_s=MAX_WAIT_S + 15,
        ),
        input_model=LaunchInput,
        output_model=LaunchOutput,
        handler=lambda a, c: _launch(launcher, a, c),
        describe=_describe(launcher),
        caller_context=True,
        precheck=_precheck(launcher),
        ask_owner=_needs_owner(launcher),
        remember=_remember(launcher),
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
