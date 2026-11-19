"""The shared interface for "Start with Windows" (the OS code is in
`integrations/adapters/pc/windows_startup.py`). Other systems: not supported yet."""

from __future__ import annotations

import sys
from typing import Protocol


class Autostart(Protocol):
    def is_enabled(self) -> bool: ...

    def enable(self) -> object: ...

    def disable(self) -> bool: ...


class Unsupported:
    def is_enabled(self) -> bool:
        return False

    def enable(self) -> object:
        raise RuntimeError("starting with the computer is only supported on Windows for now")

    def disable(self) -> bool:
        return False


def get_autostart() -> Autostart:
    if sys.platform == "win32":
        from integrations.adapters.pc.windows_startup import WindowsAutostart

        return WindowsAutostart()
    return Unsupported()
