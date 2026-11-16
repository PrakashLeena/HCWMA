"""Step 6: local MCP server and client — discovery, calls, quarantine, timeouts."""

from __future__ import annotations

import time
from typing import Any

import anyio
from mcp import Client
from pydantic import BaseModel

from integrations.mcp.client import McpToolClient, local_server_params
from integrations.mcp.server import build_server
from integrations.mcp.tool_schemas import ToolManifest, ToolSpec
from integrations.registry.capability_catalog import CapabilityCatalog


class _In(BaseModel):
    seconds: float = 0.0


class _Out(BaseModel):
    slept: float


def _sleep(args: _In) -> _Out:
    time.sleep(args.seconds)
    return _Out(slept=args.seconds)


def _spec(name: str, status: str = "approved", timeout_s: float = 5.0) -> ToolSpec:
    manifest = ToolManifest(
        name=name,
        description="sleeps",
        route="pc",
        permission_tier="T0",
        approval_required=False,
        side_effects="none",
        status=status,
        timeout_s=timeout_s,
    )
    return ToolSpec(manifest=manifest, input_model=_In, output_model=_Out, handler=_sleep)


def _catalog() -> CapabilityCatalog:
    cat = CapabilityCatalog()
    cat.register(_spec("nap", timeout_s=0.3))
    cat.register(_spec("hidden", status="quarantined"))
    return cat


def _run(fn: Any) -> Any:
    return anyio.run(fn)


def test_in_process_discovery_hides_quarantined_tools() -> None:
    async def go() -> list[str]:
        async with McpToolClient(Client(build_server(_catalog()))) as c:
            return [t.name for t in await c.list_tools()]

    assert _run(go) == ["nap"]


def test_quarantined_tool_cannot_be_called_directly() -> None:
    async def go() -> Any:
        async with McpToolClient(Client(build_server(_catalog()))) as c:
            return await c.call_tool("hidden", {})

    result = _run(go)
    assert not result.ok
    assert result.error_code == "unknown_tool"


def test_invalid_arguments_rejected_by_server() -> None:
    async def go() -> Any:
        async with McpToolClient(Client(build_server(_catalog()))) as c:
            return await c.call_tool("nap", {"seconds": "lots", "extra": 1})

    result = _run(go)
    assert not result.ok
    assert result.error_code == "invalid_arguments"


def test_slow_tool_times_out_cleanly() -> None:
    async def go() -> Any:
        async with McpToolClient(Client(build_server(_catalog()))) as c:
            return await c.call_tool("nap", {"seconds": 2.0})

    result = _run(go)
    assert not result.ok
    assert result.error_code == "timeout"


def test_real_stdio_server_discovers_and_calls_get_system_time() -> None:
    async def go() -> tuple[list[str], Any]:
        async with McpToolClient(Client(local_server_params())) as c:
            names = [t.name for t in await c.list_tools()]
            return names, await c.call_tool("get_system_time", {})

    names, result = _run(go)
    assert "get_system_time" in names
    assert result.ok, result.error
    assert result.data["utc"].endswith("+00:00")
