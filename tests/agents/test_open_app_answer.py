""" "Open <app>" and nothing more is answered by code (owner decision 2026-11-21): launch_app's
result ends the task with "Opened <app>." — no second model call and no list_windows. A request
with more to do inside the app waits (up to 15 s) for the app's window, then the model goes on.

Real App Automation card and prompt, real Router, runtime, policy layer and MCP server; a
scripted model and a fake app starter (nothing is really opened)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import pytest

from agents.router.router import RouterReply
from integrations.adapters.desktop.app_launch import (
    WINDOW_WAIT_S,
    AppLauncher,
    LaunchOutput,
    launch_answer,
    launch_tool_spec,
    load_allowlist,
    open_only,
)
from integrations.apps.access import allow_always
from integrations.registry.capability_catalog import CapabilityCatalog, default_catalog
from model.serving import GenerateResult, Message, ToolCall
from tests.agents.harness import ScriptedModel, Stack, route, stack
from tests.integrations.adapters.desktop.test_app_launch import (
    PAINT_ID,
    Clock,
    FakeStarter,
    fake_catalog,
)

AGENT = "app_automation"


@pytest.mark.parametrize(
    "words",
    [
        "open Paint", "Open paint.", "please open Paint for me", "can you open the Paint app?",
        "Hey HCWMA, could you please open Paint!", "launch Paint", "start paint now",
    ],
)  # fmt: skip
def test_open_only_requests(words: str) -> None:
    assert open_only(words, ["Paint"])


@pytest.mark.parametrize(
    "words",
    [
        "open Paint and draw a circle", "open Paint then type hello", "open Paint, click New",
        "open Paint 3D", "open Notepad", "is Paint open?", "open my report in Paint", "",
    ],
)  # fmt: skip
def test_requests_with_more_to_do_are_not_open_only(words: str) -> None:
    assert not open_only(words, ["Paint"])


def test_the_code_answer() -> None:
    base = {"app": "Paint", "opened": True, "already_running": False, "note": ""}
    assert launch_answer(LaunchOutput(**base, open_only=True)) == "Opened Paint."
    running = base | {"opened": False, "already_running": True}
    assert launch_answer(LaunchOutput(**running, open_only=True)) == "Paint was already open."
    assert launch_answer(LaunchOutput(**base, open_only=False)) is None  # the model goes on


def test_more_to_do_waits_for_the_window_up_to_15_s() -> None:
    clock = Clock()
    starter = FakeStarter()
    cat = fake_catalog()
    launcher = AppLauncher(
        starter, load_allowlist(), catalog=lambda: cat, clock=clock, sleep=clock.sleep,
        browsers=list,
    )  # fmt: skip
    target = launcher.target("Paint")
    assert launcher.wait_for_window(target) is False  # nothing appears: gives up in time
    assert clock.now == pytest.approx(WINDOW_WAIT_S, abs=0.5)

    def late_window(s: float) -> None:
        clock.sleep(s)
        if clock.now >= 4:
            starter.windows[7] = "Untitled - Paint"

    clock.now = 0.0
    launcher = AppLauncher(
        starter, load_allowlist(), catalog=lambda: cat, clock=clock, sleep=late_window,
        browsers=list,
    )  # fmt: skip
    assert launcher.wait_for_window(target) is True
    assert 4 <= clock.now < WINDOW_WAIT_S


# ---------- end to end ----------


def _catalog(starter: FakeStarter, sleep: Callable[[float], None] | None = None) -> Any:
    clock = Clock()
    cat = fake_catalog()
    launcher = AppLauncher(
        starter, load_allowlist(), catalog=lambda: cat, wait_s=30, clock=clock,
        sleep=sleep or clock.sleep, browsers=list,
    )  # fmt: skip
    catalog = CapabilityCatalog()
    catalog.register(launch_tool_spec(launcher, status="approved"))
    full = default_catalog()
    for name in ("list_windows", "read_window_text"):
        spec = full.get(name)
        if spec is not None:
            catalog.register(spec)
    return catalog


def _run(tmp: Path, words: str, agent: Any, starter: FakeStarter) -> tuple[RouterReply, Stack]:
    paint = fake_catalog().find("Paint")[0]
    allow_always(paint.id, paint.name, source="settings")  # no card in these tests
    model = ScriptedModel(router=route(steps=[{"agent": AGENT, "goal": words}]), agent=agent)

    async def go() -> tuple[RouterReply, Stack]:
        async with stack(tmp / "run", model, catalog=_catalog(starter)) as s:
            return await s.router.handle([Message(role="user", content=words)]), s

    return anyio.run(go)


def _launch_then(*rest: GenerateResult) -> Any:
    script = [GenerateResult(text="", tool_calls=[ToolCall(name="launch_app",
                                                           arguments={"app": "Paint"})]),
              *rest]  # fmt: skip
    return lambda _m, _k: script.pop(0)


def _tools_run(s: Stack) -> list[str]:
    return [r.tool for r in s.audit.read_all() if r.event == "tool_call" and r.outcome == "ok"]


def test_open_paint_is_answered_by_code_after_one_model_call(tmp_path: Path) -> None:
    starter = FakeStarter()
    reply, s = _run(tmp_path, "open Paint", _launch_then(), starter)
    assert "Opened Paint." in reply.reply
    assert starter.started == [PAINT_ID]
    assert len(s.model.agent_calls()) == 1  # no second model call
    assert _tools_run(s) == ["launch_app"]  # no list_windows


def test_open_paint_and_more_waits_for_the_window_then_the_model_goes_on(tmp_path: Path) -> None:
    starter = FakeStarter(on_start=lambda _id: starter.windows.update({3: "Loading…"}))
    clock = Clock()  # the splash counts as "opened"; Paint's own window comes 3 s later

    def later(s_: float) -> None:
        clock.sleep(s_)
        if clock.now >= 3:
            starter.windows[4] = "Untitled - Paint"

    seen: list[str] = []

    def after_launch(messages: list[Message], _k: Any) -> GenerateResult:
        seen.append(messages[-1].content or "")
        return GenerateResult(text="Paint is open; its window is ready for the next step.")

    script = _launch_then()

    def agent(messages: list[Message], kwargs: Any) -> GenerateResult:
        return (
            script(messages, kwargs)
            if not seen and len(messages) <= 2
            else after_launch(messages, kwargs)
        )

    paint = fake_catalog().find("Paint")[0]
    allow_always(paint.id, paint.name, source="settings")
    words = "open Paint and type hello"
    model = ScriptedModel(router=route(steps=[{"agent": AGENT, "goal": words}]), agent=agent)

    async def go() -> tuple[RouterReply, Stack]:
        async with stack(tmp_path / "run", model, catalog=_catalog(starter, later)) as s:
            return await s.router.handle([Message(role="user", content=words)]), s

    reply, s = anyio.run(go)
    assert len(s.model.agent_calls()) == 2  # the model continues with the rest
    assert "Opened Paint." not in reply.reply
    body = seen[0].split("\n", 1)[1].rsplit("\n</tool_output>", 1)[0]
    assert json.loads(body)["data"]["window_ready"] is True  # the model is told the window is there
    assert 4 in starter.windows  # it waited until Paint's own window was there
