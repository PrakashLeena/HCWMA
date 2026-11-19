"""Bring a window to the front on Windows, shared by App Automation and WhatsApp (step 15).

Windows' foreground lock refuses a focus change asked for by a process that is not in front
(the HCWMA window usually is, because the owner just clicked Approve there). A tap of the
Alt key lifts that lock for the next `SetForegroundWindow` call. Tried a few times; the
caller is told whether the window really is in front, and refuses to act if it is not.
"""

from __future__ import annotations

import ctypes
import time
from collections.abc import Callable

SW_RESTORE = 9
VK_MENU = 0x12
KEYEVENTF_KEYUP = 2
GA_ROOT = 2


def foreground_root() -> int:
    """The top-level window that has the keyboard focus now."""
    user32 = ctypes.windll.user32
    return int(user32.GetAncestor(user32.GetForegroundWindow(), GA_ROOT))


def bring_to_front(
    handle: int,
    set_active: Callable[[], object],
    *,
    attempts: int = 3,
    settle_s: float = 0.3,
    in_front: Callable[[], int] = foreground_root,
) -> bool:
    """True once the window `handle` is the foreground window. `set_active` is the UI
    Automation activation (`control.SetActive`), tried first on each attempt."""
    user32 = ctypes.windll.user32
    if user32.IsIconic(handle):
        user32.ShowWindow(handle, SW_RESTORE)
    for _attempt in range(attempts):
        set_active()
        time.sleep(settle_s)
        if in_front() == handle:
            return True
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        user32.SetForegroundWindow(handle)
        time.sleep(settle_s)
        if in_front() == handle:
            return True
    return False
