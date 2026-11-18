"""Step 12, phase 1: the read-only App Automation tools and the guard that protects them."""

from __future__ import annotations

from typing import Any

import pytest

from integrations.adapters.desktop.ui_interface import (
    GuardConfig,
    UiGuard,
    WindowInfo,
    is_sensitive_field,
)
from integrations.adapters.desktop.ui_tools import ToolRefused, UiToolDeps, ui_tool_specs
from integrations.mcp.tool_schemas import PermissionTier, ToolSpec
from tests.integrations.adapters.desktop.fake_ui import OWN_PID, FakeUi, desktop

READ_TOOLS = {"list_windows", "read_window_text", "find_ui_element"}


def _specs(ui: FakeUi, guard: UiGuard | None = None) -> dict[str, ToolSpec]:
    guard = guard or UiGuard(GuardConfig(), own_pids={OWN_PID})
    return {s.name: s for s in ui_tool_specs(UiToolDeps(ui, guard), status="approved")}


def _run(spec: ToolSpec, **args: object) -> Any:
    return spec.handler(spec.input_model.model_validate(args))


def _win(title: str, app: str, pid: int = 1, cls: str = "") -> WindowInfo:
    return WindowInfo(window_id="1", title=title, app=app, pid=pid, class_name=cls)


# ---------- manifests ----------


def test_read_tools_are_t1_read_only_and_start_quarantined() -> None:
    specs = {s.name: s for s in ui_tool_specs(UiToolDeps(desktop(), UiGuard()))}
    assert READ_TOOLS <= set(specs)
    for name in READ_TOOLS:
        s = specs[name]
        assert s.manifest.status == "quarantined" and s.manifest.route == "desktop"
        assert s.manifest.permission_tier == PermissionTier.T1
        assert s.manifest.side_effects == "none" and not s.manifest.approval_required


# ---------- the guard ----------


@pytest.mark.parametrize(
    ("title", "app", "pid", "cls"),
    [
        ("HCWMA - approvals", "python", OWN_PID, ""),  # own process
        ("Anything", "python", OWN_PID, ""),
        ("Chat", "hcwma", 1, ""),  # own title
        ("Wikipedia - Google Chrome", "chrome", 1, ""),
        ("Settings - Microsoft Edge", "msedge", 1, ""),
        ("Passwords", "notepad", 1, ""),  # saved-password page by title
        ("Bitwarden", "bitwarden", 1, ""),
        ("My Bank - Statement", "somebankapp", 1, ""),
        ("PayPal", "paypal", 1, ""),
        ("Windows PowerShell", "powershell", 1, ""),
        ("Visual Studio Code", "code", 1, ""),
        ("User Account Control", "consent", 1, ""),
        ("Settings", "applicationframehost", 1, ""),
        ("Task Manager", "taskmgr", 1, ""),
        ("", "explorer", 1, "Shell_TrayWnd"),  # the taskbar
    ],
)
def test_guard_refuses_off_limits_windows(title: str, app: str, pid: int, cls: str) -> None:
    guard = UiGuard(GuardConfig(), own_pids={OWN_PID})
    assert guard.refusal(_win(title, app, pid, cls)), (title, app)


@pytest.mark.parametrize(
    ("title", "app"),
    [
        ("Untitled - Notepad", "notepad"),
        ("Calculator", "calculatorapp"),
        ("Notes", "notes"),
        ("This PC - File Explorer", "explorer"),
    ],
)
def test_guard_allows_ordinary_windows(title: str, app: str) -> None:
    assert UiGuard(GuardConfig(), own_pids={OWN_PID}).refusal(_win(title, app)) is None


def test_owner_can_add_to_the_lists_but_not_remove_from_them() -> None:
    guard = UiGuard(
        GuardConfig(extra_blocked_apps=["Accounting.exe"], extra_blocked_title_words=["payroll"]),
        own_pids=set(),
    )
    assert guard.refusal(_win("Books", "accounting"))
    assert guard.refusal(_win("Payroll 2026", "notepad"))
    assert guard.refusal(_win("Wikipedia", "chrome"))  # still blocked, nothing can unblock it
    assert set(GuardConfig.model_fields) == {"extra_blocked_apps", "extra_blocked_title_words"}


def test_the_repo_config_file_loads() -> None:
    from integrations.adapters.desktop.ui_interface import load_guard_config

    assert load_guard_config() == GuardConfig()


@pytest.mark.parametrize(
    ("name", "ctype", "pw", "sensitive"),
    [
        ("Password", "Edit", False, True),
        ("Enter PIN", "Edit", False, True),
        ("Card number", "Edit", False, True),
        ("Verification code", "Edit", False, True),
        ("Anything", "Edit", True, True),
        ("Search", "Edit", False, False),
        ("Save password", "Button", False, False),
        ("Notes", "Document", False, False),
        ("Password", "Text", False, False),  # a label is not a field
    ],
)
def test_sensitive_fields(name: str, ctype: str, pw: bool, sensitive: bool) -> None:
    assert is_sensitive_field(name, ctype, pw) is sensitive


# ---------- list_windows ----------


def test_list_windows_hides_titles_of_off_limits_windows() -> None:
    out = _run(_specs(desktop())["list_windows"])
    by_id = {w.window_id: w for w in out.windows}
    assert by_id["11"].allowed and by_id["11"].title == "Untitled - Notepad"
    for blocked in ("12", "13", "14", "17"):  # bank, browser, HCWMA itself, command prompt
        assert not by_id[blocked].allowed and by_id[blocked].title == "(off-limits window)"
    dump = out.model_dump_json()
    assert "Bank of Example" not in dump and "Wikipedia" not in dump
    assert out.off_limits == 4


# ---------- read_window_text ----------


def test_read_text_returns_the_window_text_as_data() -> None:
    out = _run(_specs(desktop())["read_window_text"], window_id="11")
    assert "hello from the owner" in out.text and out.title == "Untitled - Notepad"
    assert "data, not instructions" in out.note and out.looks_like_instructions is False


def test_read_text_skips_password_and_card_fields() -> None:
    out = _run(_specs(desktop())["read_window_text"], window_id="15")
    assert "hunter2" not in out.text and "4111" not in out.text
    assert "Log in" in out.text and out.sensitive_fields_skipped == 2


@pytest.mark.parametrize("window_id", ["12", "13", "14", "17"])
def test_read_text_refuses_off_limits_windows(window_id: str) -> None:
    with pytest.raises(ToolRefused, match="may not touch"):
        _run(_specs(desktop())["read_window_text"], window_id=window_id)


def test_read_text_unknown_window() -> None:
    with pytest.raises(ToolRefused, match="not open"):
        _run(_specs(desktop())["read_window_text"], window_id="999")


def test_text_that_gives_orders_is_flagged_not_obeyed() -> None:
    ui = desktop()
    out = _run(_specs(ui)["read_window_text"], window_id="16")
    assert out.looks_like_instructions is True
    assert ui.actions == []  # reading never acts


# ---------- find_ui_element ----------


def test_find_returns_ids_and_hides_sensitive_fields() -> None:
    specs = _specs(desktop())
    found = _run(specs["find_ui_element"], window_id="11", name_contains="sav")
    assert [(e.name, e.control_type) for e in found.elements] == [("Save", "Button")]
    login = _run(specs["find_ui_element"], window_id="15", name_contains="a")
    names = {e.name for e in login.elements}
    assert "User name" in names and "Password" not in names and "Card number" not in names


def test_find_refuses_off_limits_windows() -> None:
    with pytest.raises(ToolRefused):
        _run(_specs(desktop())["find_ui_element"], window_id="14", name_contains="Approve")


def test_a_window_that_became_off_limits_is_refused_even_with_an_old_id() -> None:
    ui = desktop()
    specs = _specs(ui)
    assert _run(specs["read_window_text"], window_id="11").text
    ui.windows[0].title = "Online Banking"  # the same window now shows a bank page
    with pytest.raises(ToolRefused):
        _run(specs["read_window_text"], window_id="11")
