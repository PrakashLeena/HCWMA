"""Step 18 in the tray: Pause model, Heavy tasks only when plugged in, Start with Windows,
Back up now, the recovery notice, low-resource warnings, and a second start showing the
window. Built for real on Qt's "offscreen" platform."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.desktop.shell import Tray, make_icon  # noqa: E402
from tests.app.desktop.test_shell import FakeBackend  # noqa: E402


class FakeAutostart:
    def __init__(self) -> None:
        self.on = False
        self.fail = False

    def is_enabled(self) -> bool:
        return self.on

    def enable(self) -> object:
        if self.fail:
            raise PermissionError
        self.on = True
        return "HCWMA.lnk"

    def disable(self) -> bool:
        was, self.on = self.on, False
        return was


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication(["hcwma-test"])  # type: ignore[return-value]


def _tray(backend: FakeBackend, **kw: object) -> tuple[Tray, list[str], list[int]]:
    opened: list[int] = []
    tray = Tray(backend, make_icon(), lambda: opened.append(1), lambda: None, **kw)  # type: ignore[arg-type]
    shown: list[str] = []
    tray.icon.showMessage = lambda _title, text, *_: shown.append(text)  # type: ignore[method-assign]
    return tray, shown, opened


def test_pause_model_toggles_and_relabels(qt_app: QApplication) -> None:
    backend = FakeBackend()
    tray, shown, _ = _tray(backend)
    tray.pause_action.trigger()  # checks it
    assert backend.paused and tray.pause_action.isChecked()
    assert tray.pause_action.text() == "Resume model" and "unloaded" in shown[-1]
    tray.pause_action.trigger()
    assert not backend.paused and tray.pause_action.text() == "Pause model"

    backend.reachable = False
    tray.pause_action.trigger()
    assert not tray.pause_action.isChecked() and "Could not reach" in shown[-1]


def test_plugged_in_only_setting_follows_the_backend(qt_app: QApplication) -> None:
    backend = FakeBackend()
    backend.plugged_only = True
    tray, shown, _ = _tray(backend)
    assert tray.plugged_action.isChecked()  # read from the saved setting at start
    tray.plugged_action.trigger()
    assert backend.plugged_only is False and "battery too" in shown[-1]
    tray.plugged_action.trigger()
    assert backend.plugged_only is True and "plugged in" in shown[-1]


def test_start_with_windows_toggle(qt_app: QApplication) -> None:
    autostart = FakeAutostart()
    tray, shown, _ = _tray(FakeBackend(), autostart=autostart)
    assert tray.autostart_action.isEnabled() and not tray.autostart_action.isChecked()
    tray.autostart_action.trigger()
    assert autostart.on and tray.autostart_action.isChecked() and "sign in" in shown[-1]
    tray.autostart_action.trigger()
    assert not autostart.on and not tray.autostart_action.isChecked()
    autostart.fail = True
    tray.autostart_action.trigger()
    assert not tray.autostart_action.isChecked() and "Could not change" in shown[-1]

    no_support, _, _ = _tray(FakeBackend())
    assert not no_support.autostart_action.isEnabled()


def test_back_up_now_and_health(qt_app: QApplication) -> None:
    backend = FakeBackend()
    pages: list[int] = []
    tray, shown, _ = _tray(backend, on_health=lambda: pages.append(1))
    tray.backup_action.trigger()
    assert backend.backups == 1 and "full-20261119-120000" in shown[-1]
    tray.health_action.trigger()
    assert pages == [1]


def test_recovery_notice_after_an_unclean_exit(qt_app: QApplication) -> None:
    backend = FakeBackend()
    tray, shown, _ = _tray(backend)
    assert tray.check_recovery() == "" and shown == []
    backend.recovery = '1 request(s) were interrupted ("x"). They were NOT run again.'
    assert "NOT run again" in tray.check_recovery() and shown == [backend.recovery]


def test_low_resources_warn_once_per_new_problem(qt_app: QApplication) -> None:
    backend = FakeBackend()
    tray, shown, _ = _tray(backend)
    backend.warnings = [
        "Only 0.8 GB of RAM is free (HCWMA needs at least 1 GB). Close some programs."
    ]
    assert len(tray.check_resources()) == 1 and "refused" in shown[-1]
    assert tray.check_resources() == []  # still low: not repeated every minute
    backend.warnings = []
    tray.check_resources()
    backend.warnings = [
        "Only 0.7 GB of RAM is free (HCWMA needs at least 1 GB). Close some programs."
    ]
    assert len(tray.check_resources()) == 1  # low again after recovering: warned again


def test_a_second_start_brings_the_window_forward(qt_app: QApplication) -> None:
    backend = FakeBackend()
    tray, shown, opened = _tray(backend)
    tray.check_approvals()
    assert opened == []
    backend.show = True
    tray.check_approvals()
    assert opened == [1] and "already running" in shown[-1]


@pytest.mark.skipif(sys.platform != "win32", reason="the Startup-folder shortcut is Windows only")
def test_windows_startup_shortcut_on_and_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from integrations.adapters.pc import windows_startup
    from integrations.adapters.pc.windows_startup import SHORTCUT_NAME, WindowsAutostart

    trashed: list[str] = []
    monkeypatch.setattr(windows_startup, "send2trash", trashed.append)
    folder = tmp_path / "Startup"
    autostart = WindowsAutostart(folder=folder)
    assert not autostart.is_enabled() and autostart.disable() is False
    link = autostart.enable()
    assert link == folder / SHORTCUT_NAME and autostart.is_enabled()
    program, arguments, workdir = autostart.read_target()  # type: ignore[misc]
    assert program.lower().endswith(("pythonw.exe", "python.exe"))
    assert arguments == "-m app.desktop"
    assert Path(workdir) == windows_startup.REPO_ROOT
    assert autostart.disable() is True and trashed == [str(link)]  # Recycle Bin, not erased
    assert [p.name for p in folder.iterdir()] == [SHORTCUT_NAME]  # (the fake bin left it)


def test_startup_folder_is_the_owners_own() -> None:
    if sys.platform != "win32":
        pytest.skip("Windows only")
    from integrations.adapters.pc.windows_startup import startup_folder

    folder = startup_folder()
    assert folder.parts[-3:] == ("Start Menu", "Programs", "Startup")
    assert str(folder).lower().startswith(os.environ["APPDATA"].lower())
