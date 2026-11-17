"""The window and the tray icon (master document 4.1, 9.1, 11).

- The window shows the local chat page and nothing else: navigation to any other address is
  refused, so the API token handed to the page can never reach another site.
- The token is handed over in memory (the page's sessionStorage, in an off-the-record
  browser profile). It is never written to disk and the owner does not paste it.
- The tray icon has "Stop everything", tells the owner when an approval is waiting, and
  keeps HCWMA running when the window is closed.

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


class TrayBackend(Protocol):
    """What the tray needs from `app.desktop.backend.BackendClient`."""

    def pending_approvals(self) -> list[dict[str, Any]]: ...

    def stop_everything(self) -> dict[str, int] | None: ...


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

    def __init__(self, backend: TrayBackend, icon: QIcon, on_open: Any, on_quit: Any) -> None:
        self._backend = backend
        self._on_open = on_open
        self._known: set[str] = set()
        self.icon = QSystemTrayIcon(icon)
        self.icon.setToolTip(APP_NAME)

        self.menu = QMenu()
        self.open_action = QAction("Open HCWMA")
        self.stop_action = QAction("Stop everything")
        self.quit_action = QAction("Quit HCWMA")
        self.open_action.triggered.connect(on_open)
        self.stop_action.triggered.connect(self.stop_everything)
        self.quit_action.triggered.connect(on_quit)
        self.menu.addAction(self.open_action)
        self.menu.addAction(self.stop_action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)

        self.timer = QTimer()
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self.check_approvals)

    def show(self) -> None:
        self.icon.show()
        self.timer.start()

    def notify(self, text: str) -> None:
        self.icon.showMessage(APP_NAME, text, self.icon.icon(), 6000)

    def stop_everything(self) -> str:
        message = stop_message(self._backend.stop_everything())
        self.notify(message)
        return message

    def check_approvals(self) -> list[str]:
        """Bring the window forward when a new approval prompt is waiting."""
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
        tray.icon.hide()
        window.close()
        app.quit()

    tray = Tray(backend, icon, on_open=window.bring_forward, on_quit=quit_app)
    window.on_first_hide = lambda: tray.notify(
        "HCWMA is still running here. Right-click this icon for Stop everything or Quit."
    )
    tray.show()
    window.show()
    try:
        return int(app.exec())
    finally:
        on_exit()
