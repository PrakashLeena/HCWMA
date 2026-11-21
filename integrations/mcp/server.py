"""Local MCP server (master document 6.4). Transport: stdio only — no network listener.

The gateway (`agents/runtime/policy.py`) starts this as a child process and is its only
client. Permission checks happen in the gateway before a call is sent; this server is the
executor and adds defence in depth: only `approved` tools are listed or callable, arguments
are re-validated, and every handler runs under its manifest timeout.

    python -m integrations.mcp.server     # normally started by the gateway, not by hand
"""

from __future__ import annotations

import logging
import sys
from functools import partial
from typing import Any

import anyio
import mcp_types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from pydantic import ValidationError

from integrations.apps.adapters import adapter_refusal
from integrations.mcp.tool_schemas import CALLER_KEY, CallerContext, ToolResult
from integrations.registry.capability_catalog import CapabilityCatalog, default_catalog

SERVER_NAME = "hcwma-local-tools"
log = logging.getLogger(__name__)


def _as_call_result(result: ToolResult) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(text=result.to_text())],
        structured_content=result.model_dump(mode="json"),
        is_error=not result.ok,
    )


async def execute(catalog: CapabilityCatalog, name: str, arguments: dict[str, Any]) -> ToolResult:
    spec = catalog.get(name)
    if spec is None or spec.manifest.status != "approved":
        return ToolResult.failure("unknown_tool", f"no approved tool named '{name}'")
    off = adapter_refusal(name)  # defence in depth: the gateway checked this already
    if off is not None:
        return ToolResult.failure("denied", off)
    arguments = dict(arguments)
    raw_caller = arguments.pop(CALLER_KEY, None)
    caller: CallerContext | None = None
    if spec.caller_context:
        try:
            caller = CallerContext.model_validate(raw_caller)
        except ValidationError:
            return ToolResult.failure("invalid_arguments", f"'{name}' needs the caller context")
    elif raw_caller is not None:
        return ToolResult.failure("invalid_arguments", "invalid arguments: _caller")
    try:
        args = spec.input_model.model_validate(arguments)
    except ValidationError as exc:
        fields = sorted({".".join(map(str, e["loc"])) or "<root>" for e in exc.errors()})
        return ToolResult.failure("invalid_arguments", f"invalid arguments: {', '.join(fields)}")
    call = (
        partial(spec.handler, args, caller) if spec.caller_context else partial(spec.handler, args)
    )
    try:
        with anyio.fail_after(spec.manifest.timeout_s):
            output = await anyio.to_thread.run_sync(call, abandon_on_cancel=True)
        output = spec.output_model.model_validate(output)
    except TimeoutError:
        return ToolResult.failure("timeout", f"'{name}' exceeded {spec.manifest.timeout_s}s")
    except Exception as exc:  # a tool must never crash the server or leak a raw traceback
        log.exception("tool %s failed", name)
        return ToolResult.failure("tool_error", f"{type(exc).__name__}: {str(exc)[:200]}")
    return ToolResult.success(output)


def build_server(catalog: CapabilityCatalog) -> Server[Any]:
    async def on_list_tools(
        ctx: Any, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[s.mcp_tool() for s in catalog.callable_tools()])

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        result = await execute(catalog, params.name, params.arguments or {})
        return _as_call_result(result)

    return Server(
        SERVER_NAME, version="0.1.0", on_list_tools=on_list_tools, on_call_tool=on_call_tool
    )


async def serve_stdio(catalog: CapabilityCatalog | None = None) -> None:
    server = build_server(catalog or default_catalog())
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    # stdout carries the MCP protocol; logs go to stderr only.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    anyio.run(serve_stdio)


if __name__ == "__main__":
    main()
