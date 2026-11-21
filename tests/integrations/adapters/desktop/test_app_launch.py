"""Opening apps (owner decisions 2026-11-20 and 2026-11-21): the block list, the app catalog,
the owner's overrides, one attempt at a time, launch_app's rules, and WhatsApp being opened by
its own tools. Every launch is mocked; the app list is a fake scan."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from integrations import activity
from integrations.adapters.desktop.app_launch import (
    CONFIG_PATH,
    AppEntry,
    AppLauncher,
    LaunchFailed,
    LaunchInput,
    LaunchRefused,
    build_allowlist,
    launch_tool_spec,
    load_allowlist,
)
from integrations.adapters.desktop.whatsapp_tools import (
    ReadChatInput,
    WhatsAppToolDeps,
    WhatsAppUnavailable,
    whatsapp_tool_specs,
)
from integrations.adapters.pc.windows_apps import parse_start_apps, pick_app_id
from integrations.adapters.pc.windows_whatsapp import NOT_LINKED
from integrations.apps.access import allow_always, is_always_allowed
from integrations.apps.catalog import (
    Catalog,
    InstalledProgram,
    RawScan,
    Shortcut,
    StartApp,
    StorePackage,
    build_catalog,
)
from integrations.mcp.tool_schemas import CallerContext, PermissionTier
from tests.integrations.adapters.desktop.fake_whatsapp import sample

WA_ID = "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"
CALC_ID = "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
PAINT_ID = "Microsoft.Paint_8wekyb3d8bbwe!App"
WINDOWS = r"C:\Windows"


def fake_scan(tmp: Path | None = None) -> RawScan:
    """A small PC: ordinary apps, apps that must be blocked, and noise."""
    spot = str(tmp / "Spotify.exe") if tmp else r"C:\Apps\Spotify\Spotify.exe"
    return RawScan(
        start_apps=[
            StartApp(name="Calculator", app_id=CALC_ID),
            StartApp(name="Paint", app_id=PAINT_ID),
            StartApp(name="WhatsApp", app_id=WA_ID),
            StartApp(name="Photos", app_id="Microsoft.Windows.Photos_8wekyb3d8bbwe!App"),
            StartApp(name="Photos", app_id="Microsoft.OneDrive.Photos_8wekyb3d8bbwe!App"),
            StartApp(name="Microsoft Teams", app_id="MSTeams_8wekyb3d8bbwe!MSTeams"),
            StartApp(name="Spotify", app_id="Spotify.Desktop"),
            StartApp(name="Google Chrome", app_id="Chrome"),
            StartApp(name="Microsoft Edge", app_id="MSEdge"),
            StartApp(name="Microsoft Store", app_id="Microsoft.WindowsStore_8wekyb3d8bbwe!App"),
            StartApp(name="Terminal", app_id="Microsoft.WindowsTerminal_8wekyb3d8bbwe!App"),
            StartApp(
                name="Command Prompt", app_id="{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\cmd.exe"
            ),
            StartApp(
                name="Registry Editor", app_id="{F38BF404-1D43-42F2-9305-67DE0B28FC23}\\regedit.exe"
            ),
            StartApp(
                name="Settings",
                app_id="windows.immersivecontrolpanel_cw5n1h2txyewy!microsoft.windows.immersivecontrolpanel",
            ),
            StartApp(name="Bitwarden", app_id="com.bitwarden.desktop"),
            StartApp(name="My Bank", app_id="Vendor.MyBank_abc!App"),
            StartApp(
                name="Quick Assist", app_id="MicrosoftCorporationII.QuickAssist_8wekyb3d8bbwe!App"
            ),
            StartApp(name="Steam", app_id="Valve.Steam"),
            StartApp(name="HCWMA", app_id="hcwma.desktop"),
            StartApp(name="Help page", app_id="https://example.com/help"),
        ],
        packages=[
            StorePackage(
                name="Microsoft.WindowsCalculator",
                family=CALC_ID.split("!")[0],
                publisher="CN=Microsoft Corporation, O=Microsoft Corporation, C=US",
            ),
            StorePackage(
                name="5319275A.WhatsAppDesktop",
                family=WA_ID.split("!")[0],
                publisher="CN=24803D75-212C-471A-BC57-9EF86AB91435",
            ),
            StorePackage(
                name="Microsoft.VCLibs", family="Microsoft.VCLibs_8wekyb3d8bbwe", framework=True
            ),
        ],  # fmt: skip
        shortcuts=[
            Shortcut(name="Spotify", target=spot),
            Shortcut(name="Uninstall Spotify", target=r"C:\Apps\Spotify\unins000.exe"),
        ],
        programs=[
            InstalledProgram(name="Spotify", publisher="Spotify AB", exe=spot),
            InstalledProgram(
                name="Audacity 3.4",
                publisher="Audacity Team",
                exe=r"C:\Program Files\Audacity\Audacity.exe",
            ),
            InstalledProgram(
                name="Microsoft Visual C++ 2015 Redistributable",
                exe=r"C:\ProgramData\Package Cache\{x}\vc_redist.x64.exe",
            ),
            InstalledProgram(name="Some Tool Updater", exe=r"C:\Apps\Tool\update.exe"),
        ],  # fmt: skip
    )


def fake_catalog(tmp: Path | None = None) -> Catalog:
    return build_catalog(fake_scan(tmp), windows_dir=WINDOWS, now=1_700_000_000)


@dataclass
class FakeStarter:
    installed: list[str] = field(default_factory=lambda: [WA_ID, CALC_ID, PAINT_ID])
    running: set[str] = field(default_factory=set)
    windows: dict[int, str] = field(default_factory=dict)
    started: list[str] = field(default_factory=list)
    # what "opening" does: by default the app's process / window appears at once
    on_start: Callable[[str], None] | None = None
    notices: list[str] = field(default_factory=list)

    def find_app_id(self, prefix: str) -> str | None:
        return pick_app_id([("", a) for a in self.installed], prefix)

    def start(self, app_id: str) -> None:
        self.notices.append(activity.current())
        self.started.append(app_id)
        if self.on_start is not None:
            self.on_start(app_id)
        elif app_id == WA_ID:
            self.running.add("whatsapp.root")
        elif app_id == CALC_ID:
            self.windows[100] = "Calculator"
        elif app_id == PAINT_ID:
            self.windows[101] = "Untitled - Paint"

    def start_exe(self, exe: str) -> None:
        self.notices.append(activity.current())
        self.started.append(exe)
        if self.on_start is not None:
            self.on_start(exe)
        else:
            self.running.add(Path(exe).stem.lower())

    def is_running(self, processes: frozenset[str]) -> bool:
        return bool(self.running & processes)

    def window_titles(self) -> dict[int, str]:
        return dict(self.windows)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += s


def _launcher(
    starter: FakeStarter, clock: Clock | None = None, catalog: Catalog | None = None
) -> AppLauncher:
    clock = clock or Clock()
    cat = catalog if catalog is not None else fake_catalog()
    return AppLauncher(
        starter, load_allowlist(), catalog=lambda: cat, wait_s=30, clock=clock, sleep=clock.sleep
    )


def _caller(words: str) -> CallerContext:
    return CallerContext(agent_id="app_automation", task_id="t1", owner_words=words)


def _entry(**kw: object) -> AppEntry:
    base: dict[str, object] = {
        "id": "x", "name": "Some App", "app_id_prefix": "Vendor.SomeApp_abc", "processes": ["x"],
    }  # fmt: skip
    return AppEntry.model_validate(base | kw)


# ---------- the catalog ----------


def test_the_scan_is_merged_into_one_entry_per_app() -> None:
    cat = fake_catalog()
    names = [a.name for a in cat.apps]
    assert names.count("Photos") == 2 and "Audacity 3.4" in names
    assert "Help page" not in names  # a web link, not an app
    assert not any("Redistributable" in n or "Updater" in n for n in names)  # installers
    calc = cat.find("calculator")[0]
    assert calc.kind == "store" and calc.publisher == "Microsoft Corporation"
    assert calc.package_family == "Microsoft.WindowsCalculator_8wekyb3d8bbwe"
    spot = cat.find("Spotify")[0]
    assert spot.exe.endswith("Spotify.exe") and spot.publisher == "Spotify AB"
    assert set(spot.sources) == {"start_menu", "shortcut", "registry"}
    assert cat.find("Teams")[0].name == "Microsoft Teams"  # words of the name
    assert len({a.id for a in cat.apps}) == len(cat.apps)  # stable, unique ids
    assert fake_catalog().apps == cat.apps


@pytest.mark.parametrize(
    "name",
    [
        "Google Chrome", "Microsoft Edge", "Microsoft Store", "Terminal", "Command Prompt",
        "Registry Editor", "Settings", "Bitwarden", "My Bank", "Quick Assist", "Steam", "HCWMA",
    ],
)  # fmt: skip
def test_off_limits_apps_are_marked_blocked(name: str) -> None:
    app = next(a for a in fake_catalog().apps if a.name == name)
    assert app.blocked


def test_ordinary_apps_are_not_blocked() -> None:
    for name in ("Calculator", "Paint", "WhatsApp", "Spotify", "Microsoft Teams", "Audacity 3.4"):
        assert not next(a for a in fake_catalog().apps if a.name == name).blocked, name


# ---------- the owner's overrides (configs/apps.yaml) ----------


def test_the_shipped_overrides_are_only_whatsapp() -> None:
    allow = load_allowlist(CONFIG_PATH)
    assert sorted(allow.apps) == ["whatsapp"] and allow.rejected == {}
    assert allow.apps["whatsapp"].wait_s == 90


@pytest.mark.parametrize(
    "bad",
    [
        {"id": "chrome", "name": "Google Chrome", "app_id_prefix": "Chrome",
         "processes": ["chrome"]},
        {"name": "Microsoft Edge", "app_id_prefix": "Microsoft.MicrosoftEdge.Stable_8wekyb3d8bbwe"},
        {"name": "Terminal", "app_id_prefix": "Microsoft.WindowsTerminal_8wekyb3d8bbwe"},
        {"name": "PowerShell", "processes": ["pwsh"]},
        {"name": "1Password", "processes": ["1password"]},
        {"name": "My Bank", "app_id_prefix": "Vendor.MyBank_abc"},
        {"name": "PayPal"},
        {"name": "Microsoft Store", "app_id_prefix": "Microsoft.WindowsStore_8wekyb3d8bbwe"},
        {"name": "HCWMA"},
        {"name": "Tools", "aliases": ["regedit"]},
    ],
)  # fmt: skip
def test_off_limits_apps_can_never_be_overrides(bad: dict[str, object]) -> None:
    allow = build_allowlist([_entry(**bad)])
    assert allow.apps == {} and allow.rejected


# ---------- launching ----------


def test_a_store_app_from_the_catalog_opens_once_with_a_notice() -> None:
    starter = FakeStarter()
    result = _launcher(starter).ensure_running("Calculator")
    assert result.launched and starter.started == [CALC_ID]
    assert starter.notices == ["Opening Calculator…"] and activity.current() == ""


def test_a_desktop_app_opens_by_its_program_path(tmp_path: Path) -> None:
    (tmp_path / "Spotify.exe").write_bytes(b"MZ")
    starter = FakeStarter()
    cat = fake_catalog(tmp_path)
    spotify = cat.find("Spotify")[0]
    assert spotify.app_id == "Spotify.Desktop"  # opened through the Start menu entry first
    spotify.app_id = ""  # an app the registry knows only by its program
    result = _launcher(starter, catalog=cat).ensure_running("Spotify")
    assert result.launched and starter.started == [str(tmp_path / "Spotify.exe")]


def test_already_open_is_not_started_again() -> None:
    starter = FakeStarter(windows={7: "Calculator"})
    assert not _launcher(starter).ensure_running("calculator").launched
    assert starter.started == []


@pytest.mark.parametrize(
    ("name", "why"),
    [
        ("Google Chrome", "never opens"),
        ("Microsoft Store", "never opens"),
        ("Command Prompt", "never opens"),
        ("Photos", "more than one app"),
        ("Minecraft", "not an app HCWMA found"),
    ],
)
def test_blocked_unknown_or_unclear_apps_are_refused(name: str, why: str) -> None:
    starter = FakeStarter()
    with pytest.raises(LaunchRefused, match=why):
        _launcher(starter).ensure_running(name)
    assert starter.started == []


def test_a_catalog_entry_edited_to_a_blocked_program_is_refused_again() -> None:
    cat = fake_catalog()
    paint = next(a for a in cat.apps if a.name == "Paint")
    paint.app_id, paint.blocked = "", None
    paint.exe = r"C:\Windows\System32\cmd.exe"  # the file is data: checked again at launch
    with pytest.raises(LaunchRefused, match="never opens"):
        _launcher(FakeStarter(), catalog=cat).ensure_running("Paint")


def test_an_override_is_pinned_to_its_publisher() -> None:
    starter = FakeStarter(installed=["5319275A.WhatsAppDesktop_evilpublisher!App"])
    empty = Catalog()
    with pytest.raises(LaunchFailed, match="not installed"):
        _launcher(starter, catalog=empty).ensure_running("WhatsApp")
    assert starter.started == []


def test_a_slow_app_fails_once_with_a_message_and_is_not_retried() -> None:
    clock = Clock()
    starter = FakeStarter(on_start=lambda _id: None)  # never appears
    launcher = _launcher(starter, clock)
    with pytest.raises(LaunchFailed, match="did not finish opening within 30 s"):
        launcher.ensure_running("Calculator")
    assert starter.started == [CALC_ID] and activity.current() == ""
    with pytest.raises(LaunchFailed, match="does not keep trying"):
        launcher.ensure_running("Calculator")
    assert starter.started == [CALC_ID]  # no second attempt within the minute
    clock.now += 61
    with pytest.raises(LaunchFailed):
        launcher.ensure_running("Calculator")
    assert len(starter.started) == 2  # a later, separate request may try once more


def test_one_launch_at_a_time() -> None:
    gate, inside = threading.Event(), threading.Event()

    def slow(_id: str) -> None:
        inside.set()
        gate.wait(5)
        starter.windows[5] = "Calculator"

    starter = FakeStarter(on_start=slow)
    cat = fake_catalog()
    launcher = AppLauncher(starter, load_allowlist(), catalog=lambda: cat)
    worker = threading.Thread(target=launcher.ensure_running, args=("Calculator",))
    worker.start()
    assert inside.wait(5)
    with pytest.raises(LaunchFailed, match="already opening an app"):
        launcher.ensure_running("Paint")
    gate.set()
    worker.join(5)
    assert starter.started == [CALC_ID]


# ---------- the launch_app tool ----------


def test_launch_app_is_t2_and_only_opens_apps_the_owner_named() -> None:
    starter = FakeStarter()
    spec = launch_tool_spec(_launcher(starter), status="approved")
    assert spec.manifest.permission_tier == PermissionTier.T2
    assert not spec.manifest.approval_required and spec.remember is not None
    check = spec.precheck
    assert check is not None
    assert check(LaunchInput(app="Calculator"), _caller("open the calculator please")) is None
    assert "did not ask" in (check(LaunchInput(app="Calculator"), _caller("open paint")) or "")
    assert "never opens" in (
        check(LaunchInput(app="Google Chrome"), _caller("open google chrome")) or ""
    )
    out = spec.handler(LaunchInput(app="Calculator"), _caller("open calculator"))
    assert out.opened and starter.started == [CALC_ID]


def test_a_new_app_asks_the_owner_until_always_allowed() -> None:
    spec = launch_tool_spec(_launcher(FakeStarter()), status="approved")
    assert spec.ask_owner is not None and spec.remember is not None and spec.describe
    args = LaunchInput(app="Paint")
    assert spec.ask_owner(args) is True
    text = spec.describe(args)
    assert text.startswith("HCWMA wants to open Paint.") and "opening only" in text
    spec.remember(args)  # what "Always allow" on the card does
    paint = fake_catalog().find("Paint")[0]
    assert is_always_allowed(paint.id) and spec.ask_owner(args) is False
    assert spec.ask_owner(LaunchInput(app="Calculator")) is True  # per app, not for all
    assert spec.ask_owner(LaunchInput(app="WhatsApp")) is False  # the owner's override


def test_always_allow_cannot_unblock_a_blocked_app() -> None:
    chrome = next(a for a in fake_catalog().apps if a.name == "Google Chrome")
    allow_always(chrome.id, chrome.name, source="settings")  # even if the file says so
    with pytest.raises(LaunchRefused, match="never opens"):
        _launcher(FakeStarter()).ensure_running("Google Chrome")


# ---------- WhatsApp opened by its own tools ----------


def _wa_tools(wa: object, launcher: AppLauncher) -> dict[str, object]:
    deps = WhatsAppToolDeps(wa, launcher=launcher)  # type: ignore[arg-type]
    return {s.name: s for s in whatsapp_tool_specs(deps, status="approved")}


def test_whatsapp_closed_is_opened_then_the_chats_are_listed() -> None:
    wa = sample()
    wa.state = "not_running"
    clock = Clock()
    loading = iter(range(3))

    def opens(_id: str) -> None:
        starter.running.add("whatsapp.root")
        wa.state = "loading"

    starter = FakeStarter(on_start=opens)
    real_check = wa.check_ready

    def check_ready() -> None:  # the chat list shows after a few looks
        if wa.state == "loading" and next(loading, None) is None:
            wa.state = "ok"
        real_check()

    wa.check_ready = check_ready  # type: ignore[method-assign]
    lister = _wa_tools(wa, _launcher(starter, clock))["whatsapp_list_chats"]
    out = lister.handler(lister.input_model())
    assert starter.started == [WA_ID] and starter.notices == ["Opening WhatsApp…"]
    assert out.count == 3 and out.note.startswith("HCWMA opened WhatsApp Desktop")
    assert wa.shown == 1  # brought to the front once while it loaded, not every look
    assert clock.now < 30


def test_whatsapp_qr_screen_stops_with_the_link_message_and_no_retry() -> None:
    wa = sample()
    wa.state = "not_running"

    def opens(_id: str) -> None:
        starter.running.add("whatsapp.root")
        wa.state = "not_linked"

    starter = FakeStarter(on_start=opens)
    lister = _wa_tools(wa, _launcher(starter))["whatsapp_list_chats"]
    with pytest.raises(WhatsAppUnavailable) as err:
        lister.handler(lister.input_model())
    assert str(err.value) == NOT_LINKED
    assert NOT_LINKED == "WhatsApp needs to be linked: scan the QR code on your phone"
    with pytest.raises(WhatsAppUnavailable, match="needs to be linked"):  # asked again
        lister.handler(lister.input_model())
    assert starter.started == [WA_ID] and wa.opened == []


def test_whatsapp_running_without_a_window_is_brought_back() -> None:
    wa = sample()
    wa.state = "not_running"  # no window, but the process is still there
    starter = FakeStarter(
        running={"whatsapp.root"}, on_start=lambda _id: setattr(wa, "state", "ok")
    )
    reader = _wa_tools(wa, _launcher(starter))["whatsapp_read_chat"]
    out = reader.handler(
        ReadChatInput(chat="Sam"),
        CallerContext(
            agent_id="communication", task_id="t", owner_words="read my whatsapp chat with Sam"
        ),
    )
    assert starter.started == [WA_ID] and out.chat == "Sam"


def test_whatsapp_open_failure_is_one_plain_message() -> None:
    wa = sample()
    wa.state = "not_running"
    starter = FakeStarter(installed=[])
    lister = _wa_tools(wa, _launcher(starter, catalog=Catalog()))["whatsapp_list_chats"]
    with pytest.raises(WhatsAppUnavailable, match="WhatsApp is not installed"):
        lister.handler(lister.input_model())
    assert starter.started == []


def test_whatsapp_already_showing_is_not_launched() -> None:
    wa = sample()
    starter = FakeStarter()
    lister = _wa_tools(wa, _launcher(starter))["whatsapp_list_chats"]
    out = lister.handler(lister.input_model())
    assert starter.started == [] and not out.note.startswith("HCWMA opened")


# ---------- Windows parsing (no PowerShell) ----------


def test_start_apps_parsing_and_publisher_pinning() -> None:
    raw = (
        '[{"Name":"WhatsApp","AppID":"5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"},'
        '{"Name":"WhatsApp Beta","AppID":"5319275A.51895FA4EA97F_cv1g1gvanyjgm!App"},'
        '{"Name":"Notepad","AppID":"Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"},'
        '{"Name":"Shortcut","AppID":"C:\\\\x\\\\WhatsApp.lnk"}]'
    )
    apps = parse_start_apps(raw)
    assert len(apps) == 4
    assert pick_app_id(apps, "5319275A.WhatsAppDesktop_cv1g1gvanyjgm") == WA_ID
    assert pick_app_id(apps, "5319275A.WhatsAppDesktop") == WA_ID
    assert pick_app_id(apps, "Microsoft.WindowsCalculator_8wekyb3d8bbwe") is None
    assert parse_start_apps('{"Name":"A","AppID":"X.Y_z!App"}') == [("A", "X.Y_z!App")]
    assert parse_start_apps("") == []


# ---------- the chat-page notice (a file, because tools run in the MCP server process) ----------


def test_the_notice_is_shared_through_the_data_folder_and_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with activity.notice("Opening WhatsApp…"):
        assert activity.current() == "Opening WhatsApp…"
    assert activity.current() == ""
    activity.set_notice("Opening Notepad…")
    monkeypatch.setattr(activity.time, "time", lambda: 10**12)  # long after: a crash left it
    assert activity.current() == ""
