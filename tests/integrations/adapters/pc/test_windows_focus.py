"""Step 15: the shared foreground fix (Alt tap + SetForegroundWindow, a few tries)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from integrations.adapters.pc import windows_focus


class FakeUser32:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def IsIconic(self, handle: int) -> int:  # noqa: N802 - the Win32 name
        return 1

    def ShowWindow(self, handle: int, cmd: int) -> None:  # noqa: N802
        self.calls.append(f"show:{cmd}")

    def keybd_event(self, key: int, scan: int, flags: int, extra: int) -> None:
        self.calls.append(f"key:{key}:{flags}")

    def SetForegroundWindow(self, handle: int) -> None:  # noqa: N802
        self.calls.append("set_foreground")


@pytest.fixture
def user32(monkeypatch: pytest.MonkeyPatch) -> FakeUser32:
    fake = FakeUser32()
    monkeypatch.setattr(
        windows_focus, "ctypes", SimpleNamespace(windll=SimpleNamespace(user32=fake))
    )
    monkeypatch.setattr(windows_focus.time, "sleep", lambda _s: None)
    return fake


def test_the_alt_tap_lifts_the_foreground_lock(user32: FakeUser32) -> None:
    front = iter([111, 42])  # SetActive alone fails; after the Alt tap the window is in front
    activations: list[int] = []
    ok = windows_focus.bring_to_front(
        42, lambda: activations.append(1), in_front=lambda: next(front)
    )
    assert ok and activations == [1]
    assert user32.calls == ["show:9", "key:18:0", "key:18:2", "set_foreground"]


def test_it_gives_up_after_three_tries(user32: FakeUser32) -> None:
    ok = windows_focus.bring_to_front(42, lambda: None, in_front=lambda: 111)
    assert not ok and user32.calls.count("set_foreground") == 3


def test_app_automation_and_whatsapp_share_it() -> None:
    from pathlib import Path

    pc = Path(windows_focus.__file__).parent
    for name in ("windows_ui.py", "windows_whatsapp.py"):
        source = (pc / name).read_text(encoding="utf-8")
        assert "bring_to_front(" in source and "keybd_event" not in source, name
