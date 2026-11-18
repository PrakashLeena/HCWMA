"""Windows adapter for desktop app automation (build step 12): Microsoft UI Automation through
the `uiautomation` package. It only reads and acts; the decision about what may be touched is
made by `UiGuard` (see `integrations/adapters/desktop/ui_interface.py`) before any call here.

No screenshots, no OCR: windows are described by their accessibility tree (names, types, values).
"""

from __future__ import annotations

import ctypes
import time
from urllib.parse import quote, unquote

import psutil

from integrations.adapters.desktop.ui_interface import (
    TextRead,
    UiElement,
    UiError,
    WindowInfo,
    is_sensitive_field,
)

MAX_NODES = 1500
MAX_DEPTH = 14
MAX_NAME = 120
MAX_VALUE = 1500
WINDOW_LIMIT = 40


def _auto():  # imported late: the package only exists (and only works) on Windows
    import uiautomation as auto

    return auto


class WindowsUi:
    """`UiBackend` for Windows."""

    # ---------- windows ----------

    def _info(self, ctrl) -> WindowInfo | None:  # noqa: ANN001
        handle = ctrl.NativeWindowHandle
        if not handle:
            return None
        try:
            app = psutil.Process(ctrl.ProcessId).name().lower().removesuffix(".exe")
        except psutil.Error:
            app = "unknown"
        return WindowInfo(
            window_id=str(handle),
            title=(ctrl.Name or "")[:200],
            app=app,
            pid=ctrl.ProcessId,
            class_name=ctrl.ClassName or "",
        )

    def list_windows(self) -> list[WindowInfo]:
        auto = _auto()
        out: list[WindowInfo] = []
        with auto.UIAutomationInitializerInThread():
            for ctrl in auto.GetRootControl().GetChildren():
                if len(out) >= WINDOW_LIMIT:
                    break
                try:
                    if ctrl.IsOffscreen or not (ctrl.Name or "").strip():
                        continue
                    info = self._info(ctrl)
                except Exception:  # noqa: BLE001, S112 - a window that closes mid-look is skipped
                    continue
                if info is not None:
                    out.append(info)
        return out

    def _window_control(self, window_id: str):  # noqa: ANN202
        auto = _auto()
        try:
            handle = int(window_id)
        except ValueError:
            return None
        if not ctypes.windll.user32.IsWindow(handle):
            return None
        ctrl = auto.ControlFromHandle(handle)
        return ctrl if ctrl is not None and ctrl.NativeWindowHandle == handle else None

    def get_window(self, window_id: str) -> WindowInfo | None:
        auto = _auto()
        with auto.UIAutomationInitializerInThread():
            ctrl = self._window_control(window_id)
            return self._info(ctrl) if ctrl is not None else None

    # ---------- the accessibility tree ----------

    def _walk(self, root):  # noqa: ANN001, ANN202
        """Depth-first, deterministic: the same window gives the same order every time, which
        is what lets an element id be found again."""
        auto = _auto()
        seen = 0
        for ctrl, _depth in auto.WalkControl(root, includeTop=False, maxDepth=MAX_DEPTH):
            seen += 1
            if seen > MAX_NODES:
                return
            yield ctrl

    @staticmethod
    def _type(ctrl) -> str:  # noqa: ANN001
        return ctrl.ControlTypeName.removesuffix("Control")

    @staticmethod
    def _password(ctrl) -> bool:  # noqa: ANN001
        try:
            return bool(ctrl.IsPassword)
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _value(ctrl) -> str:  # noqa: ANN001
        try:
            pattern = ctrl.GetValuePattern()
            if pattern is not None and pattern.Value:
                return str(pattern.Value)[:MAX_VALUE]
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            pattern = ctrl.GetTextPattern()
            if pattern is not None:
                return str(pattern.DocumentRange.GetText(MAX_VALUE))
        except Exception:  # noqa: BLE001, S110
            pass
        return ""

    def read_text(self, window_id: str, max_chars: int) -> TextRead:
        auto = _auto()
        lines: list[str] = []
        total = 0
        skipped = 0
        truncated = False
        with auto.UIAutomationInitializerInThread():
            root = self._window_control(window_id)
            if root is None:
                raise UiError("that window is gone")
            deadline = time.monotonic() + 12
            for ctrl in self._walk(root):
                if time.monotonic() > deadline:
                    truncated = True
                    break
                try:
                    if ctrl.IsOffscreen:
                        continue
                    ctype = self._type(ctrl)
                    name = (ctrl.Name or "").strip()
                    if is_sensitive_field(name, ctype, self._password(ctrl)):
                        skipped += 1
                        continue
                    value = self._value(ctrl) if ctype in ("Edit", "Document", "Text") else ""
                except Exception:  # noqa: BLE001, S112 - one odd element must not stop the read
                    continue
                shown = value.strip() if value and value.strip() != name else ""
                if not name and not shown:
                    continue
                line = f"[{ctype}] {name[:MAX_NAME]}" if name else f"[{ctype}]"
                if shown:
                    line += f": {' '.join(shown.split())[:MAX_VALUE]}"
                if total + len(line) + 1 > max_chars:
                    truncated = True
                    break
                lines.append(line)
                total += len(line) + 1
        return TextRead(
            text="\n".join(lines), truncated=truncated, sensitive_fields_skipped=skipped
        )

    # ---------- elements ----------

    @staticmethod
    def _element_id(window_id: str, ctype: str, name: str, nth: int) -> str:
        return f"{window_id}~{ctype}~{quote(name, safe='')}~{nth}"

    @staticmethod
    def _parse_id(element_id: str) -> tuple[str, str, str, int] | None:
        parts = element_id.split("~")
        if len(parts) != 4 or not parts[3].isdigit():
            return None
        return parts[0], parts[1], unquote(parts[2]), int(parts[3])

    def _elements(self, window_id: str):  # noqa: ANN202
        """Yield (control, UiElement) in walk order; the n-th element with the same type and
        name is element number n."""
        root = self._window_control(window_id)
        if root is None:
            raise UiError("that window is gone")
        counts: dict[tuple[str, str], int] = {}
        for ctrl in self._walk(root):
            try:
                name = (ctrl.Name or "").strip()
                if not name or len(name) > MAX_NAME:
                    continue
                ctype = self._type(ctrl)
                nth = counts.get((ctype, name), 0)
                counts[(ctype, name)] = nth + 1
                yield (
                    ctrl,
                    UiElement(
                        element_id=self._element_id(window_id, ctype, name, nth),
                        window_id=window_id,
                        name=name,
                        control_type=ctype,
                        enabled=bool(ctrl.IsEnabled),
                        is_password=self._password(ctrl),
                    ),
                    bool(ctrl.IsOffscreen),
                )
            except Exception:  # noqa: BLE001, S112
                continue

    def find_elements(
        self, window_id: str, name_contains: str, control_type: str | None, limit: int
    ) -> list[UiElement]:
        auto = _auto()
        needle = name_contains.lower()
        found: list[UiElement] = []
        with auto.UIAutomationInitializerInThread():
            for _ctrl, element, offscreen in self._elements(window_id):
                if offscreen or needle not in element.name.lower():
                    continue
                if control_type and element.control_type.lower() != control_type.lower():
                    continue
                if is_sensitive_field(element.name, element.control_type, element.is_password):
                    continue
                found.append(element)
                if len(found) >= limit:
                    break
        return found

    def _resolve(self, element_id: str):  # noqa: ANN202
        parsed = self._parse_id(element_id)
        if parsed is None:
            raise UiError("that element_id is not valid; copy it exactly from find_ui_element")
        window_id, ctype, name, nth = parsed
        for ctrl, element, _off in self._elements(window_id):
            if element.element_id == self._element_id(window_id, ctype, name, nth):
                return ctrl, element
        return None, None

    def get_element(self, element_id: str) -> UiElement | None:
        auto = _auto()
        with auto.UIAutomationInitializerInThread():
            return self._resolve(element_id)[1]

    # ---------- actions (only called after the owner approved this exact call) ----------

    @staticmethod
    def _foreground_root() -> int:
        user32 = ctypes.windll.user32
        return int(user32.GetAncestor(user32.GetForegroundWindow(), 2))  # GA_ROOT

    def _bring_forward(self, window_id: str) -> None:
        root = self._window_control(window_id)
        if root is None:
            raise UiError("that window is gone")
        root.SetActive()
        time.sleep(0.25)
        if self._foreground_root() != int(window_id):
            raise UiError("could not bring that window to the front; nothing was done")

    def click(self, element_id: str) -> str:
        auto = _auto()
        with auto.UIAutomationInitializerInThread():
            ctrl, element = self._resolve(element_id)
            if ctrl is None or element is None:
                raise UiError("that element is no longer in the window; nothing was done")
            if not element.enabled or ctrl.IsOffscreen:
                raise UiError("that element is disabled or not visible; nothing was done")
            try:
                invoke = ctrl.GetInvokePattern()
            except Exception:  # noqa: BLE001
                invoke = None
            if invoke is not None:
                invoke.Invoke()
                return "invoked"
            self._bring_forward(element.window_id)
            rect = ctrl.BoundingRectangle
            window = self._window_control(element.window_id)
            win = window.BoundingRectangle
            cx, cy = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
            if not (win.left <= cx <= win.right and win.top <= cy <= win.bottom):
                raise UiError("that element is outside its window; nothing was done")
            ctrl.Click(x=None, y=None, simulateMove=False)
            return "clicked"

    def type_text(self, element_id: str, text: str) -> None:
        auto = _auto()
        with auto.UIAutomationInitializerInThread():
            ctrl, element = self._resolve(element_id)
            if ctrl is None or element is None:
                raise UiError("that element is no longer in the window; nothing was done")
            if not element.enabled or ctrl.IsOffscreen:
                raise UiError("that element is disabled or not visible; nothing was done")
            self._bring_forward(element.window_id)
            ctrl.SetFocus()
            time.sleep(0.15)
            focused = auto.GetFocusedControl()
            if (
                focused is None
                or focused.NativeWindowHandle != ctrl.NativeWindowHandle
                and (focused.GetRuntimeId() != ctrl.GetRuntimeId())
            ):
                raise UiError("the text box did not get the keyboard focus; nothing was typed")
            if self._foreground_root() != int(element.window_id):
                raise UiError("the window lost the focus; nothing was typed")
            for char in text:
                auto.SendUnicodeChar(char)
                time.sleep(0.004)

    def press_key(self, window_id: str, key: str) -> None:
        auto = _auto()
        keys = auto.Keys
        table = {
            "enter": [keys.VK_RETURN], "tab": [keys.VK_TAB], "escape": [keys.VK_ESCAPE],
            "backspace": [keys.VK_BACK], "delete": [keys.VK_DELETE], "space": [keys.VK_SPACE],
            "up": [keys.VK_UP], "down": [keys.VK_DOWN], "left": [keys.VK_LEFT],
            "right": [keys.VK_RIGHT], "home": [keys.VK_HOME], "end": [keys.VK_END],
            "pageup": [keys.VK_PRIOR], "pagedown": [keys.VK_NEXT],
            "shift+tab": [keys.VK_SHIFT, keys.VK_TAB],
            "ctrl+a": [keys.VK_CONTROL, ord("A")], "ctrl+c": [keys.VK_CONTROL, ord("C")],
            "ctrl+z": [keys.VK_CONTROL, ord("Z")], "ctrl+y": [keys.VK_CONTROL, ord("Y")],
            "ctrl+f": [keys.VK_CONTROL, ord("F")], "ctrl+s": [keys.VK_CONTROL, ord("S")],
        }  # fmt: skip
        codes = table.get(key)
        if codes is None:
            raise UiError(f"the key '{key}' is not allowed")
        with auto.UIAutomationInitializerInThread():
            self._bring_forward(window_id)
            for code in codes:
                auto.PressKey(code)
            for code in reversed(codes):
                auto.ReleaseKey(code)
