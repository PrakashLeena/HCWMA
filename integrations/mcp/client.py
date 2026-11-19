"""MCP client used by the tool gateway. Talks to the local server over stdio.

Agents never use this directly: they go through `agents/runtime/policy.py`, which checks
permissions and writes the audit log before and after every call.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import TracebackType
from typing import Any

import mcp_types as types
from mcp import Client, StdioServerParameters
from mcp.client.stdio import get_default_environment
from pydantic import ValidationError

from integrations.mcp.tool_schemas import ToolResult

REPO_ROOT = Path(__file__).resolve().parents[2]
# Settings the tool server must share with the backend. The MCP SDK starts the server with
# a minimal environment, so these are passed on explicitly (build step 10: both processes
# must open the same memory folder).
SHARED_ENV = (
    "HCWMA_DATA_DIR",
    "HCWMA_GENERATED_DIR",
    "HCWMA_AUDIT_PATH",
    "HCWMA_INDEX_PATH",
    "HCWMA_DISABLE_KEYRING",
)
DEFAULT_CALL_TIMEOUT_S = 60.0


def local_server_params() -> StdioServerParameters:
    """Spawn `python -m integrations.mcp.server` with this interpreter, from the repo root."""
    env = get_default_environment()
    env.update({k: os.environ[k] for k in SHARED_ENV if os.environ.get(k)})
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "integrations.mcp.server"],
        cwd=REPO_ROOT,
        env=env,
    )


class McpToolClient:
    """Thin async wrapper over `mcp.Client` that returns `ToolResult` values, never raises."""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def __aenter__(self) -> McpToolClient:
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._client.__aexit__(exc_type, exc, tb)

    async def list_tools(self) -> list[types.Tool]:
        return list((await self._client.list_tools()).tools)

    async def call_tool(
        self, name: str, arguments: dict[str, Any], timeout_s: float = DEFAULT_CALL_TIMEOUT_S
    ) -> ToolResult:
        try:
            raw = await self._client.call_tool(name, arguments, read_timeout_seconds=timeout_s)
        except Exception as exc:
            return ToolResult.failure("transport_error", f"{type(exc).__name__}: {exc}"[:300])
        try:
            return ToolResult.model_validate(raw.structured_content)
        except ValidationError:
            return ToolResult.failure("transport_error", "server returned a malformed result")
