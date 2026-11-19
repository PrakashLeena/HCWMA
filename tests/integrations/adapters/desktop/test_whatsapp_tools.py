"""Step 15: the WhatsApp rules that live in code (chat names, one send, header check)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from integrations.adapters.desktop.ui_interface import UiGuard, WindowInfo
from integrations.adapters.desktop.whatsapp_tools import (
    WHATSAPP_TOOLS_STATUS,
    SendInput,
    WhatsAppToolDeps,
    chat_problem,
    describe_send,
    header_matches,
    send_problem,
    whatsapp_tool_specs,
)
from integrations.mcp.tool_schemas import CallerContext
from integrations.registry.capability_catalog import default_catalog
from tests.integrations.adapters.desktop.fake_whatsapp import sample


def _caller(words: str, task: str = "t1") -> CallerContext:
    return CallerContext(agent_id="communication", task_id=task, owner_words=words)


@pytest.mark.parametrize(
    ("chat", "words", "ok"),
    [
        ("Sam", "Read my WhatsApp chat with Sam", True),
        ("sam", "what did SAM say on whatsapp", True),
        ("Sam", "Read my WhatsApp chat with Samantha", False),  # whole names only
        ("Family", "Read my WhatsApp chat with Sam", False),  # not the chat the owner named
        ("Message yourself", "send a whatsapp to message yourself saying hi", True),
        ("Message yourself", "send myself a whatsapp note", True),
        ("Message yourself", "Read my chat with Sam", False),
        ("S", "chat S", False),
    ],
)
def test_only_chats_the_owner_typed(chat: str, words: str, ok: bool) -> None:
    assert (chat_problem(chat, words) is None) is ok


def test_a_send_needs_the_owner_to_ask_for_one() -> None:
    assert send_problem("Tell Sam on WhatsApp I am late") is None
    assert send_problem("Send a whatsapp to Sam") is None
    assert send_problem("Read my WhatsApp chat with Sam") is not None


def test_header_must_match_exactly() -> None:
    assert header_matches("Sam", "Sam") and header_matches("sam", " Sam ")
    assert not header_matches("Sam", "Sam Work") and not header_matches("Sam", "")
    assert header_matches("Message yourself", "@owner (You)")
    assert not header_matches("Message yourself", "Sam")


def test_the_approval_prompt_shows_chat_and_the_whole_text() -> None:
    body = "line one\n" + "x" * 900
    text = describe_send(SendInput(chat="Sam", body=body))
    assert "Chat: Sam" in text and body in text and "cannot be unsent" in text
    assert "your own chat" in describe_send(SendInput(chat="Message yourself", body="hi"))


def test_send_input_limits() -> None:
    with pytest.raises(ValidationError):
        SendInput(chat="Sam", body="x" * 1001)
    with pytest.raises(ValidationError):
        SendInput(chat="Sam\nFamily", body="hi")  # one chat only
    with pytest.raises(ValidationError):
        SendInput(chat="Sam", body="hi\tthere\x1b")


def _spec(deps: WhatsAppToolDeps, name: str):  # noqa: ANN202
    return next(s for s in whatsapp_tool_specs(deps, status="approved") if s.name == name)


def test_one_send_per_task_and_header_checked() -> None:
    wa = sample()
    deps = WhatsAppToolDeps(wa)
    send = _spec(deps, "whatsapp_send")
    caller = _caller("send Sam a whatsapp saying hi")
    args = SendInput(chat="Sam", body="hi")
    assert send.precheck(args, caller) is None  # type: ignore[misc]
    send.handler(args, caller)
    assert wa.sent == [("Sam", "hi")]
    assert send.precheck(args, caller) is not None  # type: ignore[misc]
    with pytest.raises(PermissionError):
        send.handler(args, caller)
    assert len(wa.sent) == 1


def test_a_different_open_chat_stops_the_send() -> None:
    wa = sample()
    wa.header_override = "Sam Work"
    send = _spec(WhatsAppToolDeps(wa), "whatsapp_send")
    with pytest.raises(RuntimeError, match="not 'Sam'"):
        send.handler(SendInput(chat="Sam", body="hi"), _caller("send Sam hi on whatsapp"))
    assert wa.sent == []


@pytest.mark.parametrize("state", ["not_running", "not_linked"])
def test_not_running_or_not_linked_is_said_plainly(state: str) -> None:
    wa = sample()
    wa.state = state
    lister = _spec(WhatsAppToolDeps(wa), "whatsapp_list_chats")
    with pytest.raises(RuntimeError, match="not running" if state == "not_running" else "linked"):
        lister.handler(lister.input_model())


def test_app_automation_may_not_touch_whatsapp() -> None:
    guard = UiGuard(own_pids=[1])
    for app, title in [("whatsapp.root", "WhatsApp Beta"), ("whatsapp", "WhatsApp"),
                       ("someapp", "WhatsApp")]:  # fmt: skip
        reason = guard.refusal(WindowInfo(window_id="9", title=title, app=app, pid=50))
        assert reason and "Communication agent" in reason


def test_manifests_tiers_and_status() -> None:
    catalog = default_catalog()
    tiers = {n: catalog.get(n).manifest for n in ("whatsapp_list_chats", "whatsapp_read_chat",
                                                    "whatsapp_send")}  # fmt: skip
    assert tiers["whatsapp_list_chats"].permission_tier.value == "T1"
    assert tiers["whatsapp_read_chat"].permission_tier.value == "T1"
    assert tiers["whatsapp_send"].permission_tier.value == "T3"
    assert tiers["whatsapp_send"].approval_required
    assert not tiers["whatsapp_read_chat"].approval_required
    assert {m.status for m in tiers.values()} == {WHATSAPP_TOOLS_STATUS}


def test_read_returns_newest_first_and_says_so() -> None:
    wa = sample()
    read = _spec(WhatsAppToolDeps(wa), "whatsapp_read_chat")
    out = read.handler(read.input_model(chat="Sam"), _caller("read my whatsapp chat with Sam"))
    assert out.order.startswith("newest first")
    assert [m.text for m in out.messages] == ["Yes, 1pm", "Are we still on for lunch?"]
