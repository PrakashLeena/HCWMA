"""Step 15: parsing WhatsApp Desktop's accessibility tree (tokens shaped like the real app's)."""

from __future__ import annotations

from integrations.adapters.pc.windows_whatsapp import (
    chat_row_from_tokens,
    header_from,
    is_self_name,
    messages_from_rows,
)


def test_a_chat_row_gives_name_time_preview_and_unread() -> None:
    row = chat_row_from_tokens(
        [
            ("DataItem", "Sam 11:27 Sibi : GIF"),
            ("Group", ""),
            ("Text", "Sam"),
            ("Text", "11:27"),
            ("Text", "Sibi"),
            ("Text", ": "),
            ("Text", "GIF"),
            ("Text", "2 unread messages"),
        ]
    )
    assert row is not None
    assert (row.name, row.time, row.preview, row.unread) == ("Sam", "11:27", "Sibi: GIF", 2)


def test_a_search_result_name_split_by_highlighting_is_joined() -> None:
    row = chat_row_from_tokens(
        [
            ("Text", "@"),
            ("Text", "owner"),
            ("Text", "  "),
            ("Text", "(You)"),
            ("Text", "11/11/2026"),
            ("Text", "a link"),
        ]  # fmt: skip
    )
    assert row is not None and row.name == "@owner (You)" and row.time == "11/11/2026"
    assert is_self_name(row.name) and not is_self_name("Youssef")


def test_pinned_and_dated_rows() -> None:
    row = chat_row_from_tokens(
        [
            ("Group", "Pinned chat"),
            ("Text", "sem2"),
            ("Text", "Yesterday"),
            ("Text", "You"),
            ("Text", ": "),
            ("Text", "0700"),
        ]  # fmt: skip
    )
    assert row is not None
    assert (row.name, row.time, row.preview) == ("sem2", "Yesterday", "You: 0700")
    assert chat_row_from_tokens([("Group", "Muted chat")]) is None


def test_message_rows_keep_sender_time_and_continue_the_sender() -> None:
    rows = [
        [("Text", "Today")],
        [("Group", "Sam:"), ("Text", "Hello there"), ("Text", "11:22")],
        [("Text", "Second line from Sam"), ("Text", "11:23")],
        [
            ("Group", "You:"),
            ("Text", "Hi"),
            ("Button", "11:24 Read "),
            ("Text", "11:24"),
            ("Group", " Read "),
            ("Image", "wds-ic-read"),
        ],  # fmt: skip
        [
            ("Group", "Sam:"),
            ("Button", "Open picture"),
            ("Text", "11:25"),
            ("Button", "Forward media"),
        ],  # fmt: skip
        [("Text", "Use WhatsApp on your phone to see older messages")],  # a notice, no time
    ]
    msgs = messages_from_rows(rows)
    assert [(m.sender, m.time, m.text) for m in msgs] == [
        ("Sam", "11:22", "Hello there"),
        ("Sam", "11:23", "Second line from Sam"),
        ("You", "11:24", "Hi"),
        ("Sam", "11:25", "[picture]"),
    ]
    assert msgs[0].date == "Today"


def test_a_repeated_link_preview_title_is_kept_once() -> None:
    msgs = messages_from_rows(
        [
            [
                ("Group", "You:"),
                ("Text", "Forwarded"),
                ("Text", "Title"),
                ("Text", "Title"),
                ("Text", "www.example.com"),
                ("Text", "23:36"),
            ]
        ]  # fmt: skip
    )
    assert msgs[0].text == "Title www.example.com"


def test_header_comes_from_the_contact_info_button() -> None:
    assert header_from("Contact info @owner (You)", []) == "@owner (You)"
    assert header_from("Contact info +94 70 000 0000", []) == "+94 70 000 0000"
    assert header_from("Group info Family", []) == "Family"
    assert header_from("", ["Family", "Amma, Appa, You"]) == "Family Amma, Appa, You"
