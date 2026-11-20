"""Windows adapter for WhatsApp Desktop (build step 15), through Microsoft UI Automation (the
`uiautomation` package, as App Automation uses). No unofficial WhatsApp library, no scraping.

WhatsApp Desktop (Microsoft Store app, publisher id 5319275A) shows WhatsApp's web interface in
a WebView2 control, whose accessibility tree has: a `DataGrid` "Chat list" (or "Search
results." while searching), a `Group` with automation id "main" for the open chat (header
button "Contact info <name>", message rows as `DataItem`s, the composer `Edit` "Type a message
to <name>").

Only the policy-free mechanics live here. What may be read or sent, and to whom, is decided in
`integrations/adapters/desktop/whatsapp_tools.py` before anything here is called. The parsing of
rows is plain Python over (control type, name) tokens so it is tested without a screen.
"""

from __future__ import annotations

import ctypes
import re
import time
from collections.abc import Callable, Iterable
from pathlib import PureWindowsPath

import psutil

from integrations.adapters.desktop.whatsapp_tools import (
    ChatMessage,
    ChatRow,
    WhatsAppRefused,
    WhatsAppUnavailable,
    normalize_name,
)
from integrations.adapters.pc.windows_focus import bring_to_front

Token = tuple[str, str]  # (control type without "Control", name)

WHATSAPP_APPS = frozenset({"whatsapp.root", "whatsapp"})
PUBLISHER_DIR = "5319275a."  # WindowsApps\5319275A.<package>_... : WhatsApp's Store package
NOT_RUNNING = "WhatsApp Desktop is not running. Open WhatsApp on this PC and try again."
NOT_LINKED = "WhatsApp needs to be linked: scan the QR code on your phone"
NOT_READY = "WhatsApp Desktop is open but its chat list is not showing yet. Try again shortly."

_TIME = re.compile(r"^\d{1,2}[:.]\d{2}(\s?[APap]\.?[Mm]\.?)?$")
_DATE = re.compile(
    r"^(\d{1,2}/\d{1,2}/\d{2,4}|today|yesterday|monday|tuesday|wednesday|thursday|friday"
    r"|saturday|sunday)$",
    re.IGNORECASE,
)
_UNREAD = re.compile(r"^(\d+) unread messages?$", re.IGNORECASE)
_STATUS = re.compile(r"^\s*(read|delivered|sent|pending)\s*$", re.IGNORECASE)
_SKIP_BUTTONS = frozenset(
    {
        "forward media", "view", "save as...", "quoted message", "react", "download",
        "context menu", "play", "pause", "open in app", "forward", "reply",
    }
)  # fmt: skip
_SKIP_TEXTS = frozenset({"forwarded", "edited", "you", "message yourself"})
MAX_TEXT = 600
MAX_PREVIEW = 120


def _clean(text: str) -> str:
    return " ".join(text.replace(" ", " ").split())


# ---------- parsing (pure, tested) ----------


def chat_row_from_tokens(tokens: Iterable[Token]) -> ChatRow | None:
    """A chat-list row: Text tokens are name parts, then the time, then the preview."""
    name_parts: list[str] = []
    when = ""
    rest: list[str] = []
    unread = 0
    for kind, raw in tokens:
        if kind not in ("Text", "Image"):
            continue
        text = _clean(raw)
        if kind == "Image":  # emoji in names/previews; icons are named like "wds-ic-..."
            if text and not text.startswith(("wds-", "default-")) and len(text) <= 4:
                (rest if when else name_parts).append(text)
            continue
        match = _UNREAD.match(text)
        if match:
            unread = int(match.group(1))
            continue
        if not when and (_TIME.match(text) or _DATE.match(text)) and name_parts:
            when = text
            continue
        if when:
            if text:
                rest.append(text)
        else:
            name_parts.append(raw.replace(" ", " "))
    name = _clean("".join(name_parts)) if name_parts else ""
    if not name:
        return None
    preview = _clean(" ".join(rest)).replace(" :", ":")[:MAX_PREVIEW]
    return ChatRow(name=name, time=when, preview=preview, unread=unread)


def messages_from_rows(rows: Iterable[list[Token]]) -> list[ChatMessage]:
    """Message rows of the open chat, oldest first. A row without its own sender label
    continues the previous sender; a row that is only a date sets the date."""
    out: list[ChatMessage] = []
    sender = ""
    date = ""
    for tokens in rows:
        row_sender = ""
        when = ""
        parts: list[str] = []
        for kind, raw in tokens:
            text = _clean(raw)
            if not text:
                continue
            if kind == "Group":
                if text.endswith(":") and len(text) > 1 and not row_sender:
                    row_sender = text[:-1].strip()
                continue
            if kind == "Button":
                low = text.lower()
                if low in _SKIP_BUTTONS or _TIME.match(text.split(" ")[0]):
                    continue
                if low == "open picture":
                    parts.append("[picture]")
                elif low.startswith(("pdf ", "document ")) or " • " in text:
                    parts.append(f"[file: {text[:80]}]")
                elif "voice message" in low or low.startswith("play voice"):
                    parts.append("[voice message]")
                continue
            if kind == "Image":
                if len(text) <= 4 and not text.startswith("wds-"):
                    parts.append(text)
                continue
            if kind != "Text":
                continue
            if _TIME.match(text):
                when = text
                continue
            if _STATUS.match(text) or text.lower() in _SKIP_TEXTS or text in (":",):
                continue
            if parts and parts[-1] == text:  # link previews repeat their title
                continue
            parts.append(text)
        if not when:
            if len(parts) == 1 and _DATE.match(parts[0]):
                date = parts[0]
            continue  # not a message (date separator, notices)
        if row_sender:
            sender = row_sender
        body = " ".join(parts)[:MAX_TEXT] or "[media]"
        out.append(ChatMessage(sender=sender or "?", time=when, date=date, text=body))
    return out


def header_from(button_name: str, texts: list[str]) -> str:
    """The open chat's name from the header button ("Contact info X") or its first texts."""
    for prefix in ("Contact info ", "Group info ", "Profile details ", "Business info "):
        if button_name.startswith(prefix):
            return _clean(button_name[len(prefix) :])
    return _clean(" ".join(t for t in texts[:2] if _clean(t).lower() != "message yourself"))


def is_self_name(name: str) -> bool:
    return normalize_name(name).endswith("(you)")


# ---------- the live backend ----------


def _auto():  # imported late: the package only exists (and only works) on Windows
    import uiautomation as auto

    return auto


def _ctype(ctrl) -> str:  # noqa: ANN001
    return ctrl.ControlTypeName.removesuffix("Control")


class WindowsWhatsApp:
    """`WhatsAppBackend` for Windows. Every method finds the window afresh."""

    def __init__(self, settle_s: float = 1.2) -> None:
        self._settle = settle_s

    # ----- the window -----

    @staticmethod
    def _genuine(pid: int) -> bool:
        """The process is WhatsApp's own Store package, not a window that merely says so."""
        try:
            proc = psutil.Process(pid)
            name = proc.name().lower().removesuffix(".exe")
            exe = PureWindowsPath(proc.exe())
        except psutil.Error:
            return False
        parts = [p.lower() for p in exe.parts]
        return (
            name in WHATSAPP_APPS
            and "windowsapps" in parts
            and any(p.startswith(PUBLISHER_DIR) for p in parts)
        )

    def _window(self):  # noqa: ANN202
        auto = _auto()
        for ctrl in auto.GetRootControl().GetChildren():
            try:
                if "whatsapp" not in (ctrl.Name or "").lower():
                    continue
                if self._genuine(ctrl.ProcessId):
                    return ctrl
            except Exception:  # noqa: BLE001, S112 - a window closing mid-look
                continue
        raise WhatsAppUnavailable(NOT_RUNNING, reason="not_running")

    @staticmethod
    def _find(root, kind: str, *, name: str | None = None, aid: str | None = None,  # noqa: ANN001, ANN205
              starts: str | None = None, depth: int = 40):  # fmt: skip
        auto = _auto()
        for ctrl, _d in auto.WalkControl(root, includeTop=False, maxDepth=depth):
            try:
                if _ctype(ctrl) != kind:
                    continue
                if name is not None and ctrl.Name != name:
                    continue
                if aid is not None and ctrl.AutomationId != aid:
                    continue
                if starts is not None and not (ctrl.Name or "").startswith(starts):
                    continue
                return ctrl
            except Exception:  # noqa: BLE001, S112
                continue
        return None

    def _chat_grid(self, window):  # noqa: ANN001, ANN202
        grid = self._find(window, "DataGrid", name="Chat list")
        if grid is not None:
            return grid
        text = " ".join(
            (c.Name or "") for c, _ in _auto().WalkControl(window, maxDepth=40)
            if _ctype(c) in ("Text", "Button", "Hyperlink")
        ).lower()  # fmt: skip
        if any(w in text for w in ("log in", "scan", "qr code", "link with phone", "link a")):
            raise WhatsAppUnavailable(NOT_LINKED, reason="not_linked")
        raise WhatsAppUnavailable(NOT_READY, reason="not_ready")

    @staticmethod
    def _tokens(ctrl, depth: int = 14) -> list[Token]:  # noqa: ANN001
        out: list[Token] = []
        for child, _d in _auto().WalkControl(ctrl, includeTop=False, maxDepth=depth):
            try:
                out.append((_ctype(child), child.Name or ""))
            except Exception:  # noqa: BLE001, S112
                continue
        return out

    @staticmethod
    def _foreground_root() -> int:
        user32 = ctypes.windll.user32
        return int(user32.GetAncestor(user32.GetForegroundWindow(), 2))  # GA_ROOT

    def _bring_forward(self, window) -> None:  # noqa: ANN001
        handle = window.NativeWindowHandle
        if not bring_to_front(handle, window.SetActive, in_front=self._foreground_root):
            raise WhatsAppRefused("could not bring WhatsApp to the front; nothing was done")

    def _keys(self, window, *codes: int) -> None:  # noqa: ANN001
        auto = _auto()
        if self._foreground_root() != window.NativeWindowHandle:
            raise WhatsAppRefused("WhatsApp lost the focus; stopped")
        for code in codes:
            auto.PressKey(code)
        for code in reversed(codes):
            auto.ReleaseKey(code)
        time.sleep(0.08)

    # ----- health (build step 18) -----

    def link_state(self) -> tuple[bool, str]:
        """(linked, plain status) for the health page. Reads only whether the chat list is
        showing; no chat names or messages are read, and nothing is clicked or focused."""
        auto = _auto()
        with auto.UIAutomationInitializerInThread():
            try:
                self._chat_grid(self._window())
            except WhatsAppUnavailable as exc:
                return False, str(exc)
        return True, "linked (chat list showing)"

    def check_ready(self) -> None:
        """Raise `WhatsAppUnavailable` (with its reason) unless the chat list is showing.
        Reads nothing else; clicks and focuses nothing."""
        auto = _auto()
        with auto.UIAutomationInitializerInThread():
            self._chat_grid(self._window())

    def show_window(self) -> bool:
        """Bring WhatsApp's window to the front (after HCWMA opened it: a window that starts
        behind others is not drawn by WebView2, so its chat list never appears in the
        accessibility tree). Clicks and types nothing. True when it is in front."""
        auto = _auto()
        with auto.UIAutomationInitializerInThread():
            try:
                window = self._window()
                return bring_to_front(
                    window.NativeWindowHandle, window.SetActive, in_front=self._foreground_root
                )
            except WhatsAppUnavailable:
                return False

    # ----- list -----

    def list_chats(self, limit: int) -> list[ChatRow]:
        auto = _auto()
        with auto.UIAutomationInitializerInThread():
            grid = self._chat_grid(self._window())
            rows: list[ChatRow] = []
            for item in grid.GetChildren():
                if len(rows) >= limit:
                    break
                try:
                    if _ctype(item) != "DataItem" or item.IsOffscreen:
                        continue
                except Exception:  # noqa: BLE001, S112
                    continue
                row = chat_row_from_tokens(self._tokens(item))
                if row is not None:
                    rows.append(row)
            return rows

    # ----- open -----

    def _main(self, window):  # noqa: ANN001, ANN202
        return self._find(window, "Group", aid="main")

    def _header(self, window) -> str:  # noqa: ANN001
        main = self._main(window)
        if main is None:
            return ""
        button = self._find(main, "Button", depth=6)
        texts = [n for k, n in self._tokens(main, depth=5) if k == "Text"]
        return header_from(button.Name if button is not None else "", texts)

    def _matching_row(self, grid, name: str, self_chat: bool):  # noqa: ANN001, ANN202
        wanted = normalize_name(name)
        for item in grid.GetChildren():
            try:
                if _ctype(item) != "DataItem" or item.IsOffscreen:
                    continue
            except Exception:  # noqa: BLE001, S112
                continue
            row = chat_row_from_tokens(self._tokens(item))
            if row is None:
                continue
            if (self_chat and is_self_name(row.name)) or normalize_name(row.name) == wanted:
                return item
        return None

    def _search_box(self, window):  # noqa: ANN001, ANN202
        for ctrl, _d in _auto().WalkControl(window, includeTop=False, maxDepth=40):
            try:
                if _ctype(ctrl) == "Edit" and not (ctrl.Name or "").startswith("Type a message"):
                    return ctrl
            except Exception:  # noqa: BLE001, S112
                continue
        return None

    def _clear_search(self, window) -> None:  # noqa: ANN001
        auto = _auto()
        box = self._search_box(window)
        if box is None:
            return
        box.Click(simulateMove=False)
        time.sleep(0.2)
        self._keys(window, auto.Keys.VK_CONTROL, ord("A"))
        self._keys(window, auto.Keys.VK_BACK)
        self._keys(window, auto.Keys.VK_ESCAPE)

    def open_chat(self, name: str, *, self_chat: bool) -> str:
        auto = _auto()
        with auto.UIAutomationInitializerInThread():
            window = self._window()
            grid = self._chat_grid(window)
            current = self._header(window)
            if current and (
                (self_chat and is_self_name(current))
                or normalize_name(current) == normalize_name(name)
            ):
                return current
            self._bring_forward(window)
            row = self._matching_row(grid, name, self_chat)
            searched = False
            if row is None:  # not in the visible list: WhatsApp's own search box
                box = self._search_box(window)
                if box is None:
                    raise WhatsAppRefused(f"could not find the chat '{name}'")
                box.Click(simulateMove=False)
                time.sleep(0.2)
                if self._foreground_root() != window.NativeWindowHandle:
                    raise WhatsAppRefused("WhatsApp lost the focus; stopped")
                query = "You" if self_chat else name
                for char in query:
                    auto.SendUnicodeChar(char)
                    time.sleep(0.01)
                searched = True
                time.sleep(self._settle)
                results = self._find(window, "DataGrid", name="Search results.")
                row = self._matching_row(results, name, self_chat) if results else None
            try:
                if row is None:
                    raise WhatsAppRefused(
                        f"no chat named exactly '{name}' was found in WhatsApp; nothing was done"
                    )
                row.Click(simulateMove=False)
                deadline = time.monotonic() + 4
                header = ""
                while time.monotonic() < deadline:
                    time.sleep(0.3)
                    header = self._header(window)
                    if header and (
                        (self_chat and is_self_name(header))
                        or normalize_name(header) == normalize_name(name)
                    ):
                        break
                return header
            finally:
                if searched:
                    self._clear_search(window)

    # ----- read -----

    def read_open_chat(self, limit: int) -> list[ChatMessage]:
        auto = _auto()
        with auto.UIAutomationInitializerInThread():
            main = self._main(self._window())
            if main is None:
                raise WhatsAppRefused("no chat is open in WhatsApp")
            rows: list[list[Token]] = []
            row_depth = -1  # depth of the DataItem we are inside, -1 when outside any row
            for ctrl, depth in auto.WalkControl(main, includeTop=False, maxDepth=10):
                try:
                    kind = _ctype(ctrl)
                    if row_depth >= 0 and depth <= row_depth:
                        row_depth = -1
                    if kind == "DataItem":
                        rows.append(self._tokens(ctrl))
                        row_depth = depth
                    elif row_depth < 0 and kind == "Text" and _DATE.match(_clean(ctrl.Name)):
                        rows.append([("Text", ctrl.Name)])  # a date separator between rows
                except Exception:  # noqa: BLE001, S112
                    continue
            return messages_from_rows(rows)[-limit:]

    # ----- send (only after the owner approved this exact message) -----

    def _composer(self, window):  # noqa: ANN001, ANN202
        main = self._main(window)
        return self._find(main, "Edit", starts="Type a message", depth=14) if main else None

    @staticmethod
    def _composer_text(box) -> str:  # noqa: ANN001
        try:
            return str(box.GetTextPattern().DocumentRange.GetText(-1))
        except Exception:  # noqa: BLE001
            try:
                return str(box.GetLegacyIAccessiblePattern().Value)
            except Exception:  # noqa: BLE001
                return "?"

    def send_to_open_chat(self, check_header: Callable[[str], bool], body: str) -> None:
        auto = _auto()
        keys = auto.Keys
        with auto.UIAutomationInitializerInThread():
            window = self._window()
            self._bring_forward(window)
            if not check_header(self._header(window)):
                raise WhatsAppRefused("the open chat is not the one you named; nothing was sent")
            box = self._composer(window)
            if box is None:
                raise WhatsAppRefused("the message box was not found; nothing was sent")
            if self._composer_text(box).strip():
                raise WhatsAppRefused(
                    "the message box already has text in it; nothing was sent (clear it first)"
                )
            box.Click(simulateMove=False)
            time.sleep(0.2)
            typed = False
            try:
                for i, line in enumerate(body.split("\n")):
                    if i:
                        self._keys(window, keys.VK_SHIFT, keys.VK_RETURN)
                    if self._foreground_root() != window.NativeWindowHandle:
                        raise WhatsAppRefused("WhatsApp lost the focus while typing; not sent")
                    for char in line:
                        auto.SendUnicodeChar(char)
                        typed = True
                        time.sleep(0.004)
                time.sleep(0.3)
                box = self._composer(window)
                shown = self._composer_text(box) if box is not None else ""
                if _clean(shown) != _clean(body):
                    raise WhatsAppRefused(
                        "the message box does not hold exactly the approved text; not sent"
                    )
                if not check_header(self._header(window)):  # last look before Enter
                    raise WhatsAppRefused("the open chat changed; nothing was sent")
                self._keys(window, keys.VK_RETURN)
                typed = False
            finally:
                if typed:  # stopped half way: take our text back out of the box
                    try:
                        box = self._composer(window)
                        if box is not None:
                            box.Click(simulateMove=False)
                            self._keys(window, keys.VK_CONTROL, ord("A"))
                            self._keys(window, keys.VK_BACK)
                    except Exception:  # noqa: BLE001, S110
                        pass
            time.sleep(0.8)
            box = self._composer(window)
            if box is not None and _clean(self._composer_text(box)) == _clean(body):
                raise WhatsAppRefused("WhatsApp did not take the message (still in the box)")
