"""A fake WhatsApp Desktop for the step 15 tests: chats and messages in memory, every action
recorded, so a test can prove that nothing was opened or sent."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from integrations.adapters.desktop.whatsapp_tools import (
    ChatMessage,
    ChatRow,
    WhatsAppRefused,
    WhatsAppUnavailable,
)
from integrations.adapters.pc.windows_whatsapp import NOT_LINKED, NOT_RUNNING, is_self_name

SELF_NAME = "@owner (You)"


@dataclass
class FakeWhatsApp:
    chats: dict[str, list[ChatMessage]] = field(default_factory=dict)
    state: str = "ok"  # ok | not_running | not_linked
    header_override: str | None = None  # the chat that really opens (to simulate a wrong one)
    open: str = ""
    opened: list[str] = field(default_factory=list)
    sent: list[tuple[str, str]] = field(default_factory=list)

    def _check(self) -> None:
        if self.state == "not_running":
            raise WhatsAppUnavailable(NOT_RUNNING)
        if self.state == "not_linked":
            raise WhatsAppUnavailable(NOT_LINKED)

    def list_chats(self, limit: int) -> list[ChatRow]:
        self._check()
        return [
            ChatRow(name=name, time="11:00", preview=(msgs[-1].text if msgs else ""))
            for name, msgs in list(self.chats.items())[:limit]
        ]

    def open_chat(self, name: str, *, self_chat: bool) -> str:
        self._check()
        target = next(
            (
                n
                for n in self.chats
                if (self_chat and is_self_name(n)) or n.casefold() == name.casefold()
            ),
            None,
        )
        if target is None:
            raise WhatsAppRefused(f"no chat named exactly '{name}' was found in WhatsApp")
        self.open = self.header_override or target
        self.opened.append(self.open)
        return self.open

    def read_open_chat(self, limit: int) -> list[ChatMessage]:
        self._check()
        return self.chats.get(self.open, [])[-limit:]

    def send_to_open_chat(self, check_header: Callable[[str], bool], body: str) -> None:
        self._check()
        if not check_header(self.open):
            raise WhatsAppRefused("the open chat is not the one you named; nothing was sent")
        self.sent.append((self.open, body))
        self.chats.setdefault(self.open, []).append(
            ChatMessage(sender="You", time="11:59", text=body)
        )


def sample() -> FakeWhatsApp:
    return FakeWhatsApp(
        chats={
            SELF_NAME: [ChatMessage(sender="You", time="10:00", text="buy bread")],
            "Sam": [
                ChatMessage(sender="Sam", time="10:01", text="Are we still on for lunch?"),
                ChatMessage(sender="You", time="10:02", text="Yes, 1pm"),
            ],
            "Family": [ChatMessage(sender="Amma", time="09:00", text="Call me tonight")],
        }
    )
