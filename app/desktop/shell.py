"""The window and the tray icon (master document 4.1, 9.1, 11).

- The window shows the local chat page and nothing else: navigation to any other address is
  refused, so the API token handed to the page can never reach another site.
- The token is handed over in memory (the page's sessionStorage, in an off-the-record
  browser profile). It is never written to disk and the owner does not paste it.
- The tray icon has "Stop everything", tells the owner when an approval is waiting, shows a
  desktop notification when a planner reminder or deadline is due (build step 15), and
  keeps HCWMA running when the window is closed.
- Build step 18: "Health" opens the health page; "Pause model" unloads Qwen; "Heavy tasks
  only when plugged in" and "Start with Windows" are the owner's toggles; "Back up now".
  After an unclean exit the tray says which requests were interrupted (never re-run), it
  warns when RAM or disk runs low, and a second start of HCWMA brings this window forward.

Approving or denying happens in the window (the approval card in the chat page).
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMainWindow, QMenu, QSystemTrayIcon

APP_NAME = "HCWMA"
TOKEN_STORAGE_KEY = "hcwma-token"  # noqa: S105 - the storage key name, same as app/web/chat.js
POLL_MS = 2000
REMINDER_POLL_MS = 15_000
RESOURCE_POLL_MS = 60_000


class TrayBackend(Protocol):
    """What the tray needs from `app.desktop.backend.BackendClient`."""

    def pending_approvals(self) -> list[dict[str, Any]]: ...

    def due_reminders(self) -> list[dict[str, Any]]: ...

    def stop_everything(self) -> dict[str, int] | None: ...

    def show_requested(self) -> bool: ...

    def recovery_message(self) -> str: ...

    def resource_warnings(self) -> list[str]: ...

    def set_model_paused(self, paused: bool) -> bool | None: ...

    def settings(self) -> dict[str, Any]: ...

    def set_plugged_in_only(self, on: bool) -> bool | None: ...

    def backup_now(self) -> str | None: ...


class AutostartToggle(Protocol):
    """`app.desktop.autostart.Autostart`."""

    def is_enabled(self) -> bool: ...

    def enable(self) -> object: ...

    def disable(self) -> bool: ...


def is_backend_url(url: str, base_url: str) -> bool:
    """True only for pages of the local backend itself (same scheme, host and port)."""
    target, base = QUrl(url), QUrl(base_url)
    return (
        target.scheme() == base.scheme() == "http"
        and target.host() == base.host() == "127.0.0.1"
        and target.port() == base.port()
        and not target.userName()
    )


def token_script(token: str, base_url: str) -> str:
    """JavaScript that gives the chat page its token, and only the chat page."""
    return (
        "(function () {"
        f"if (window.location.origin !== {json.dumps(base_url)}) return;"
        f"try {{ window.sessionStorage.setItem({json.dumps(TOKEN_STORAGE_KEY)}, "
        f"{json.dumps(token)}); }} catch (e) {{}}"
        "})();"
    )


def stop_message(report: dict[str, int] | None) -> str:
    if report is None:
        return "Could not reach HCWMA to stop it."
    if not report.get("stopped_tasks") and not report.get("cancelled_approvals"):
        return "Nothing was running."
    return "Stopped. Everything HCWMA was doing has been halted."


def reminder_message(due: list[dict[str, Any]]) -> str:
    """The notification text for due planner items (titles are shown as plain text)."""
    lines = []
    for item in due[:5]:
        label = "Deadline" if item.get("kind") == "deadline" else "Reminder"
        lines.append(f"{label}: {item.get('title', '')} ({item.get('due_text', '')})")
    return "\n".join(lines)


def make_icon() -> QIcon:
    """Drawn in code, so the repository needs no image file."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#2563eb"))
    painter.setPen(QColor("#2563eb"))
    painter.drawEllipse(2, 2, 60, 60)
    painter.setPen(QColor("#ffffff"))
    font = QFont()
    font.setPixelSize(40)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "H")
    painter.end()
    return QIcon(pixmap)


class Tray:
    """Tray icon: open the window, stop everything, quit. Watches for approval prompts."""

    def __init__(
        self,
        backend: TrayBackend,
        icon: QIcon,
        on_open: Any,
        on_quit: Any,
        *,
        on_health: Any = None,
        autostart: AutostartToggle | None = None,
    ) -> None:
        self._backend = backend
        self._on_open = on_open
        self._autostart = autostart
        self._known: set[str] = set()
        self._warned: set[str] = set()
        self.icon = QSystemTrayIcon(icon)
        self.icon.setToolTip(APP_NAME)

        self.menu = QMenu()
        self.open_action = QAction("Open HCWMA")
        self.health_action = QAction("Health")
        self.stop_action = QAction("Stop everything")
        self.pause_action = QAction("Pause model")
        self.pause_action.setCheckable(True)
        self.plugged_action = QAction("Heavy tasks only when plugged in")
        self.plugged_action.setCheckable(True)
        self.autostart_action = QAction("Start with Windows")
        self.autostart_action.setCheckable(True)
        self.backup_action = QAction("Back up now")
        self.quit_action = QAction("Quit HCWMA")
        self.open_action.triggered.connect(on_open)
        self.health_action.triggered.connect(on_health or on_open)
        self.stop_action.triggered.connect(self.stop_everything)
        self.pause_action.triggered.connect(self.toggle_pause)
        self.plugged_action.triggered.connect(self.toggle_plugged_in_only)
        self.autostart_action.triggered.connect(self.toggle_autostart)
        self.backup_action.triggered.connect(self.backup_now)
        self.quit_action.triggered.connect(on_quit)
        self.menu.addAction(self.open_action)
        self.menu.addAction(self.health_action)
        self.menu.addAction(self.stop_action)
        self.menu.addSeparator()
        self.menu.addAction(self.pause_action)
        self.menu.addAction(self.plugged_action)
        self.menu.addAction(self.autostart_action)
        self.menu.addAction(self.backup_action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)
        self.autostart_action.setEnabled(autostart is not None)
        self.refresh_toggles()
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)

        self.timer = QTimer()
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self.check_approvals)
        self.reminder_timer = QTimer()
        self.reminder_timer.setInterval(REMINDER_POLL_MS)
        self.reminder_timer.timeout.connect(self.check_reminders)
        self.resource_timer = QTimer()
        self.resource_timer.setInterval(RESOURCE_POLL_MS)
        self.resource_timer.timeout.connect(self.check_resources)

    def show(self) -> None:
        self.icon.show()
        self.timer.start()
        self.reminder_timer.start()
        self.resource_timer.start()
        QTimer.singleShot(3000, self.check_reminders)  # missed while HCWMA was off
        QTimer.singleShot(1500, self.check_recovery)  # what the last unclean exit left
        QTimer.singleShot(5000, self.check_resources)

    # ---------- build step 18 ----------

    def refresh_toggles(self) -> None:
        settings = self._backend.settings()
        self.plugged_action.setChecked(bool(settings.get("heavy_only_when_plugged_in")))
        if self._autostart is not None:
            try:
                self.autostart_action.setChecked(self._autostart.is_enabled())
            except Exception:  # noqa: BLE001 - shown unticked
                self.autostart_action.setChecked(False)

    def check_recovery(self) -> str:
        message = self._backend.recovery_message()
        if message:
            self.icon.showMessage(
                f"{APP_NAME} recovered", message, QSystemTrayIcon.MessageIcon.Warning, 30_000
            )
        return message

    def check_resources(self) -> list[str]:
        """Warn once per new low-resource condition (RAM, disk)."""
        warnings = self._backend.resource_warnings()
        keys = ["ram" if "RAM" in w else "disk" for w in warnings]
        fresh = [w for w, key in zip(warnings, keys, strict=True) if key not in self._warned]
        self._warned = set(keys)
        if fresh:
            self.icon.showMessage(
                f"{APP_NAME}: low resources",
                " ".join(fresh) + " New requests are refused until this is fixed.",
                QSystemTrayIcon.MessageIcon.Warning,
                20_000,
            )
        return fresh

    def toggle_pause(self) -> str:
        wanted = self.pause_action.isChecked()
        result = self._backend.set_model_paused(wanted)
        if result is None:
            message = "Could not reach HCWMA to change the model."
            result = not wanted
        elif result:
            message = "Model paused: Qwen is being unloaded to free memory. New requests wait."
        else:
            message = "Model resumed: Qwen loads again on the next request."
        self.pause_action.setChecked(result)
        self.pause_action.setText("Resume model" if result else "Pause model")
        self.notify(message)
        return message

    def toggle_plugged_in_only(self) -> str:
        wanted = self.plugged_action.isChecked()
        result = self._backend.set_plugged_in_only(wanted)
        if result is None:
            self.plugged_action.setChecked(not wanted)
            message = "Could not reach HCWMA to change the setting."
        else:
            self.plugged_action.setChecked(result)
            message = (
                "Heavy tasks now run only when the laptop is plugged in."
                if result
                else "Heavy tasks run on battery too."
            )
        self.notify(message)
        return message

    def toggle_autostart(self) -> str:
        wanted = self.autostart_action.isChecked()
        if self._autostart is None:
            self.autostart_action.setChecked(False)
            return "Not supported on this computer."
        try:
            if wanted:
                self._autostart.enable()
                message = "HCWMA will start when you sign in to Windows."
            else:
                self._autostart.disable()
                message = "HCWMA will no longer start with Windows (shortcut in the Recycle Bin)."
        except Exception as exc:  # noqa: BLE001 - told to the owner
            message = f"Could not change it: {type(exc).__name__}"
        self.refresh_toggles()
        self.notify(message)
        return message

    def backup_now(self) -> str:
        name = self._backend.backup_now()
        message = f"Backup written: {name}" if name else "The backup failed; see the health page."
        self.notify(message)
        return message

    def notify(self, text: str) -> None:
        self.icon.showMessage(APP_NAME, text, self.icon.icon(), 6000)

    def stop_everything(self) -> str:
        message = stop_message(self._backend.stop_everything())
        self.notify(message)
        return message

    def check_approvals(self) -> list[str]:
        """Bring the window forward when a new approval prompt is waiting (or when a second
        start of HCWMA asked for it)."""
        if self._backend.show_requested():
            self.notify("HCWMA is already running; here it is.")
            self._on_open()
        pending = {str(p["id"]): p for p in self._backend.pending_approvals()}
        new = [pid for pid in pending if pid not in self._known]
        self._known = set(pending)
        waiting = len(pending)
        self.icon.setToolTip(f"{APP_NAME} — {waiting} approval(s) waiting" if waiting else APP_NAME)
        if new:
            first = pending[new[0]]
            self.notify(f"Approval needed: {first.get('tool', 'an action')}. Open HCWMA to answer.")
            self._on_open()
        return new

    def check_reminders(self) -> str | None:
        """Show one desktop notification for the reminders and deadlines now due."""
        due = self._backend.due_reminders()
        if not due:
            return None
        text = reminder_message(due)
        self.icon.showMessage(
            f"{APP_NAME} reminder", text, QSystemTrayIcon.MessageIcon.Information, 30_000
        )
        return text

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._on_open()


class ChatWindow(QMainWindow):
    """The chat page in a window. Closing it hides it; HCWMA keeps running in the tray."""

    def __init__(self, base_url: str, token: str, icon: QIcon) -> None:
        super().__init__()
        # Imported here so the tray and helpers above can be used (and tested) without
        # starting the embedded browser.
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript
        from PySide6.QtWebEngineWidgets import QWebEngineView

        class LocalOnlyPage(QWebEnginePage):
            def acceptNavigationRequest(  # noqa: N802 - Qt's method name
                self, url: QUrl, _type: Any, _is_main_frame: bool
            ) -> bool:
                return is_backend_url(url.toString(), base_url)

            def createWindow(self, _type: Any) -> None:  # noqa: N802 - no pop-ups
                return None

        self._base_url = base_url
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(icon)
        self.resize(900, 720)
        self.quitting = False
        self.on_first_hide: Any = None

        self._profile = QWebEngineProfile(self)  # no name = off the record: nothing on disk
        script = QWebEngineScript()
        script.setName("hcwma-token")
        script.setSourceCode(token_script(token, base_url))
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(False)
        self._profile.scripts().insert(script)

        self._page = LocalOnlyPage(self._profile, self)
        self._view = QWebEngineView(self)
        self._view.setPage(self._page)
        self.setCentralWidget(self._view)
        self._view.load(QUrl(base_url + "/"))

    def open_page(self, path: str) -> None:
        """Show one of the backend's own pages (`/status` is the health page)."""
        self._view.load(QUrl(self._base_url + path))
        self.bring_forward()

    def bring_forward(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt's method name
        if self.quitting:
            event.accept()
            return
        event.ignore()
        self.hide()
        if self.on_first_hide is not None:
            self.on_first_hide()
            self.on_first_hide = None


def run_shell(backend: TrayBackend, base_url: str, token: str, on_exit: Any) -> int:
    """Open the window and the tray icon; returns when the owner picks Quit."""
    app = QApplication.instance() or QApplication([APP_NAME])
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    icon = make_icon()
    window = ChatWindow(base_url, token, icon)

    def quit_app() -> None:
        window.quitting = True
        tray.timer.stop()
        tray.reminder_timer.stop()
        tray.resource_timer.stop()
        tray.icon.hide()
        window.close()
        app.quit()

    from app.desktop.autostart import get_autostart

    tray = Tray(
        backend,
        icon,
        on_open=window.bring_forward,
        on_quit=quit_app,
        on_health=lambda: window.open_page("/status"),
        autostart=get_autostart(),
    )
    window.on_first_hide = lambda: tray.notify(
        "HCWMA is still running here. Right-click this icon for Stop everything or Quit."
    )
    tray.show()
    window.show()
    try:
        return int(app.exec())
    finally:
        on_exit()
