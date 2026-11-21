"""Opening apps through the gateway (owner decision 2026-11-21): the approval card with
Allow once / Always allow / Deny, blocked apps never asking, adapters that are off, and every
decision audited. Real policy layer, approval service and in-process MCP server; a fake app
list and a fake starter (nothing is really opened)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from pydantic import BaseModel

from agents.runtime.policy import PolicyLayer
from integrations.adapters.desktop.app_launch import AppLauncher, launch_tool_spec, load_allowlist
from integrations.apps import access
from integrations.apps.adapters import adapters_path, load_adapters, set_adapter
from integrations.mcp.client import McpToolClient
from integrations.mcp.server import build_server
from integrations.mcp.tool_schemas import CallerContext, PermissionTier, ToolManifest, ToolSpec
from integrations.registry.capability_catalog import CapabilityCatalog
from security.approvals.approval_service import ApprovalService, PendingApproval
from security.audit.audit_logger import AuditLogger
from security.permissions.permission_manager import (
    AgentGrant,
    PermissionManager,
    load_permissions_config,
)
from tests.integrations.adapters.desktop.test_app_launch import (
    CALC_ID,
    PAINT_ID,
    Clock,
    FakeStarter,
    fake_catalog,
)

AGENT = AgentGrant(
    agent_id="app_automation",
    tools=frozenset({"launch_app", "whatsapp_list_chats"}),
    max_tier="T3",
)


class _NoArgs(BaseModel):
    pass


class _Done(BaseModel):
    ok: bool = True


def _catalog(starter: FakeStarter) -> CapabilityCatalog:
    clock = Clock()
    cat = fake_catalog()
    launcher = AppLauncher(
        starter, load_allowlist(), catalog=lambda: cat, wait_s=30, clock=clock, sleep=clock.sleep
    )
    catalog = CapabilityCatalog()
    catalog.register(launch_tool_spec(launcher, status="approved"))
    catalog.register(
        ToolSpec(
            manifest=ToolManifest(
                name="whatsapp_list_chats",
                description="stand-in",
                route="desktop",
                permission_tier="T1",
                status="approved",
            ),  # fmt: skip
            input_model=_NoArgs,
            output_model=_Done,
            handler=lambda _a: _Done(),
        )
    )
    return catalog


def _ask(
    tmp: Path,
    starter: FakeStarter,
    tool: str,
    args: dict[str, Any],
    words: str,
    decision: str | None,
) -> tuple[Any, list[PendingApproval], AuditLogger]:
    """One call through the gateway. `decision`: how the owner answers a card (None: there
    must be no card)."""
    audit = AuditLogger(tmp / "audit.jsonl")
    approvals = ApprovalService(audit, timeout_s=5)
    seen: list[PendingApproval] = []
    catalog = _catalog(starter)
    context = CallerContext(agent_id=AGENT.agent_id, task_id="t1", owner_words=words)

    async def owner() -> None:
        while not approvals.pending():
            await anyio.sleep(0.01)
        card = approvals.pending()[0]
        seen.append(card)
        assert decision is not None, "no card was expected"
        assert approvals.decide(card.id, decision)  # type: ignore[arg-type]

    async def go() -> Any:
        async with McpToolClient(Client(build_server(catalog))) as client:
            policy = PolicyLayer(
                client=client,
                catalog=catalog,
                permissions=PermissionManager(load_permissions_config()),
                audit=audit,
                approvals=approvals,
            )
            async with anyio.create_task_group() as tg:
                tg.start_soon(owner)
                result = await policy.call_tool("t1", AGENT, tool, args, context=context)
                tg.cancel_scope.cancel()
            return result

    return anyio.run(go), seen, audit


def _paint_id() -> str:
    return fake_catalog().find("Paint")[0].id


def test_a_new_app_shows_the_card_and_allow_once_opens_it_once(tmp_path: Path) -> None:
    starter = FakeStarter()
    result, seen, audit = _ask(
        tmp_path, starter, "launch_app", {"app": "Paint"}, "open Paint", "approve"
    )
    assert result.ok and starter.started == [PAINT_ID]
    assert seen[0].can_remember and seen[0].summary.startswith("HCWMA wants to open Paint.")
    assert not access.is_always_allowed(_paint_id())  # once only
    outcomes = [(r.event, r.outcome) for r in audit.read_all()]
    assert ("approval", "requested") in outcomes and ("approval", "approved") in outcomes
    # the next request asks again
    starter.windows.clear()
    _, again, _ = _ask(tmp_path, starter, "launch_app", {"app": "Paint"}, "open Paint", "approve")
    assert len(again) == 1


def test_always_allow_is_saved_and_the_next_open_does_not_ask(tmp_path: Path) -> None:
    starter = FakeStarter()
    result, seen, audit = _ask(
        tmp_path, starter, "launch_app", {"app": "Paint"}, "open Paint", "always"
    )
    assert result.ok and len(seen) == 1 and access.is_always_allowed(_paint_id())
    outcomes = [(r.event, r.tool, r.outcome) for r in audit.read_all()]
    assert ("approval", "launch_app", "approved_always") in outcomes
    assert "Always allow" in audit.read_all()[-1].reason
    starter.windows.clear()
    result, seen, _ = _ask(tmp_path, starter, "launch_app", {"app": "Paint"}, "open Paint", None)
    assert result.ok and seen == [] and starter.started == [PAINT_ID, PAINT_ID]
    # another app still asks: "Always allow" is per app
    _, seen, _ = _ask(
        tmp_path, starter, "launch_app", {"app": "Calculator"}, "open calculator", "deny"
    )
    assert len(seen) == 1 and CALC_ID not in starter.started


def test_deny_opens_nothing_and_is_audited(tmp_path: Path) -> None:
    starter = FakeStarter()
    result, seen, audit = _ask(
        tmp_path, starter, "launch_app", {"app": "Paint"}, "open Paint", "deny"
    )
    assert not result.ok and result.error_code == "approval_denied" and starter.started == []
    assert audit.read_all()[-1].outcome == "approval_denied"


@pytest.mark.parametrize("app", ["Google Chrome", "Microsoft Store", "Command Prompt", "My Bank"])
def test_blocked_apps_never_show_a_card(tmp_path: Path, app: str) -> None:
    starter = FakeStarter()
    result, seen, audit = _ask(tmp_path, starter, "launch_app", {"app": app}, f"open {app}", None)
    assert not result.ok and result.error_code == "denied" and seen == []
    assert starter.started == [] and "never opens" in (result.error or "")
    assert audit.read_all()[-1].outcome == "denied"


def test_text_the_agent_read_cannot_name_an_app(tmp_path: Path) -> None:
    starter = FakeStarter()
    result, seen, _ = _ask(
        tmp_path, starter, "launch_app", {"app": "Paint"}, "what does this window say?", None
    )
    assert not result.ok and seen == [] and starter.started == []


def test_always_allow_only_on_a_card_that_offers_it(tmp_path: Path) -> None:
    approvals = ApprovalService(AuditLogger(tmp_path / "a.jsonl"), timeout_s=2)

    async def go() -> Any:
        async with anyio.create_task_group() as tg:
            result: list[Any] = []

            async def ask() -> None:
                result.append(
                    await approvals.request(
                        task_id="t",
                        agent="a",
                        tool="move_file",
                        tier=PermissionTier.T3,
                        arguments={},
                        summary="move",
                    )  # fmt: skip
                )

            tg.start_soon(ask)
            while not approvals.pending():
                await anyio.sleep(0.01)
            card = approvals.pending()[0]
            assert not card.can_remember
            assert not approvals.decide(card.id, "always")  # refused: not offered here
            assert approvals.decide(card.id, "approve")
        return result[0]

    answer = anyio.run(go)
    assert answer.outcome == "approved" and not answer.remember


# ---------- adapters ----------


def test_tools_of_an_adapter_that_is_off_are_refused_and_hidden(tmp_path: Path) -> None:
    set_adapter("whatsapp", False, source="settings", path=adapters_path())
    result, seen, audit = _ask(
        tmp_path, FakeStarter(), "whatsapp_list_chats", {}, "list chats", None
    )
    assert (
        not result.ok and result.error_code == "denied" and "adapter is off" in (result.error or "")
    )

    async def visible() -> list[str]:
        catalog = _catalog(FakeStarter())
        async with McpToolClient(Client(build_server(catalog))) as client:
            policy = PolicyLayer(
                client=client, catalog=catalog,
                permissions=PermissionManager(load_permissions_config()), audit=audit,
            )  # fmt: skip
            return [t.name for t in await policy.list_tools_for(AGENT)]

    assert "whatsapp_list_chats" not in anyio.run(visible)
    set_adapter("whatsapp", True, source="settings")
    assert anyio.run(visible).count("whatsapp_list_chats") == 1
    assert load_adapters().adapters["whatsapp"].on
