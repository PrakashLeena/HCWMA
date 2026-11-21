"""Auto-launch (owner decision 2026-11-20): launch_app's allowlist, the off-limits rule, one
attempt at a time, and WhatsApp being opened by its own tools. The launch is mocked."""

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
from integrations.mcp.tool_schemas import CallerContext, PermissionTier
from tests.integrations.adapters.desktop.fake_whatsapp import sample

WA_ID = "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"
CALC_ID = "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"


@dataclass
class FakeStarter:
    installed: list[str] = field(default_factory=lambda: [WA_ID, CALC_ID])
    running: set[str] = field(default_factory=set)
    started: list[str] = field(default_factory=list)
    # what "opening" does: by default the app's process appears at once
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
            self.running.add("calculatorapp")

    def is_running(self, processes: frozenset[str]) -> bool:
        return bool(self.running & processes)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += s


def _launcher(starter: FakeStarter, clock: Clock | None = None) -> AppLauncher:
    clock = clock or Clock()
    return AppLauncher(starter, load_allowlist(), wait_s=30, clock=clock, sleep=clock.sleep)


def _caller(words: str) -> CallerContext:
    return CallerContext(agent_id="app_automation", task_id="t1", owner_words=words)


def _entry(**kw: object) -> AppEntry:
    base: dict[str, object] = {
        "id": "x", "name": "Some App", "app_id_prefix": "Vendor.SomeApp_abc", "processes": ["x"],
    }  # fmt: skip
    return AppEntry.model_validate(base | kw)


# ---------- the list ----------


def test_the_shipped_list_is_whatsapp_calculator_notepad() -> None:
    allow = load_allowlist(CONFIG_PATH)
    assert sorted(allow.apps) == ["calculator", "notepad", "whatsapp"]
    assert allow.apps["whatsapp"].wait_s == 90 and allow.apps["calculator"].wait_s == 30
    assert allow.rejected == {}
    assert allow.find("calc").id == "calculator" and allow.find("WhatsApp").id == "whatsapp"
    assert allow.find("Paint") is None


@pytest.mark.parametrize(
    "bad",
    [
        {
            "id": "chrome",
            "name": "Google Chrome",
            "app_id_prefix": "Chrome",
            "processes": ["chrome"],
        },
        {"name": "Microsoft Edge", "app_id_prefix": "Microsoft.MicrosoftEdge.Stable_8wekyb3d8bbwe"},
        {"name": "Firefox", "processes": ["firefox"]},
        {"name": "Terminal", "app_id_prefix": "Microsoft.WindowsTerminal_8wekyb3d8bbwe"},
        {"name": "PowerShell", "processes": ["pwsh"]},
        {"name": "Command Prompt"},
        {"name": "VS Code", "processes": ["code"]},
        {"name": "1Password", "processes": ["1password"]},
        {"name": "Bitwarden"},
        {"name": "My Bank", "app_id_prefix": "Vendor.MyBank_abc"},
        {"name": "PayPal"},
        {"name": "Crypto Wallet"},
        {"name": "Microsoft Store", "app_id_prefix": "Microsoft.WindowsStore_8wekyb3d8bbwe"},
        {"name": "Settings", "app_id_prefix": "windows.immersivecontrolpanel_cw5n1h2txyewy"},
        {"name": "HCWMA"},
        {"name": "Tools", "aliases": ["regedit"]},
    ],
)
def test_off_limits_apps_can_never_be_on_the_list(bad: dict[str, object]) -> None:
    allow = build_allowlist([_entry(**bad)])
    assert allow.apps == {} and allow.rejected


def test_an_off_limits_entry_in_the_yaml_is_dropped_and_refused(tmp_path: Path) -> None:
    path = tmp_path / "apps.yaml"
    path.write_text(
        CONFIG_PATH.read_text(encoding="utf-8")
        + "  - id: chrome\n    name: Chrome\n    app_id_prefix: Chrome\n"
        "    processes: [chrome]\n",
        encoding="utf-8",
    )
    allow = load_allowlist(path)
    assert "chrome" in allow.rejected and "chrome" not in allow.apps
    launcher = AppLauncher(FakeStarter(installed=["Chrome!App"]), allow)
    with pytest.raises(LaunchRefused, match="not on your list"):
        launcher.ensure_running("Chrome")


# ---------- launching ----------


def test_opens_the_installed_app_once_and_shows_a_notice() -> None:
    starter = FakeStarter()
    result = _launcher(starter).ensure_running("Calculator")
    assert result.launched and starter.started == [CALC_ID]
    assert starter.notices == ["Opening Calculator…"] and activity.current() == ""


def test_already_running_is_not_started_again() -> None:
    starter = FakeStarter(running={"calculatorapp"})
    assert not _launcher(starter).ensure_running("calculator").launched
    assert starter.started == []


def test_not_installed_is_said_plainly_and_nothing_is_installed() -> None:
    starter = FakeStarter(installed=[])
    with pytest.raises(LaunchFailed, match="not installed.*never downloads or installs"):
        _launcher(starter).ensure_running("Notepad")
    assert starter.started == []


def test_a_look_alike_with_another_publisher_is_not_opened() -> None:
    starter = FakeStarter(installed=["5319275A.WhatsAppDesktop_evilpublisher!App"])
    with pytest.raises(LaunchFailed, match="not installed"):
        _launcher(starter).ensure_running("WhatsApp")
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
        starter.running.add("calculatorapp")

    starter = FakeStarter(on_start=slow)
    launcher = AppLauncher(starter, load_allowlist())
    worker = threading.Thread(target=launcher.ensure_running, args=("Calculator",))
    worker.start()
    assert inside.wait(5)
    with pytest.raises(LaunchFailed, match="already opening an app"):
        launcher.ensure_running("WhatsApp")
    gate.set()
    worker.join(5)
    assert starter.started == [CALC_ID]


# ---------- the launch_app tool ----------


def test_launch_app_is_t2_and_only_opens_apps_the_owner_named() -> None:
    starter = FakeStarter()
    spec = launch_tool_spec(_launcher(starter), status="approved")
    assert spec.manifest.permission_tier == PermissionTier.T2
    assert not spec.manifest.approval_required
    check = spec.precheck
    assert check is not None
    assert check(LaunchInput(app="Calculator"), _caller("open the calculator please")) is None
    assert "did not ask" in (check(LaunchInput(app="Calculator"), _caller("open notepad")) or "")
    assert "not on your list" in (check(LaunchInput(app="Chrome"), _caller("open chrome")) or "")
    out = spec.handler(LaunchInput(app="calc"), _caller("open calc"))
    assert out.opened and starter.started == [CALC_ID]


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
    tools = _wa_tools(wa, _launcher(starter))
    lister = tools["whatsapp_list_chats"]
    with pytest.raises(WhatsAppUnavailable) as err:
        lister.handler(lister.input_model())
    assert (
        str(err.value)
        == NOT_LINKED
        == ("WhatsApp needs to be linked: scan the QR code on your phone")
    )
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
    lister = _wa_tools(wa, _launcher(starter))["whatsapp_list_chats"]
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
