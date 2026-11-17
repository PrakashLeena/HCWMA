"""Wire the real Router: cards → registry, MCP gateway → policy layer, shared model queue."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import anyio
from mcp import Client

from agents.registry.agent_registry import AgentRegistry
from agents.router.router import Router
from agents.runtime.agent_runtime import AgentRuntime
from agents.runtime.model_queue import ModelQueue, get_model_queue
from agents.runtime.policy import PolicyLayer
from agents.runtime.stop import StopControl
from integrations.mcp.client import McpToolClient, local_server_params
from integrations.mcp.server import build_server
from integrations.registry.capability_catalog import CapabilityCatalog, default_catalog
from memory.pc_index.index import FileIndex
from memory.pc_index.watcher import IndexService
from security.approvals.approval_service import DEFAULT_APPROVAL_TIMEOUT_S, ApprovalService
from security.audit.audit_logger import AuditLogger, get_audit_logger
from security.permissions.path_check import PathPolicy
from security.permissions.permission_manager import PermissionManager, load_permissions_config


@asynccontextmanager
async def file_index_service() -> AsyncIterator[IndexService]:
    """Scan the indexed folders once, then keep the PC file index fresh until exit."""
    service = IndexService(FileIndex(), PathPolicy.from_config())
    await anyio.to_thread.run_sync(service.start)
    try:
        yield service
    finally:
        service.stop()


@asynccontextmanager
async def open_router(
    queue: ModelQueue | None = None,
    audit: AuditLogger | None = None,
    max_tokens: int = 512,
    registry: AgentRegistry | None = None,
    catalog: CapabilityCatalog | None = None,
    keep_file_index_fresh: bool = False,
    approval_timeout_s: float = DEFAULT_APPROVAL_TIMEOUT_S,
) -> AsyncIterator[Router]:
    """Start the local MCP server (stdio child process) and yield a ready Router.

    `catalog`: use these tools on an in-process MCP server instead (demos and tests).
    `keep_file_index_fresh`: also run the PC file index scan + watcher (the backend does).
    """
    config = load_permissions_config()
    registry = registry or AgentRegistry.from_dir()
    audit = audit or get_audit_logger()
    transport = Client(build_server(catalog)) if catalog else Client(local_server_params())
    async with AsyncExitStack() as stack:
        if keep_file_index_fresh:
            await stack.enter_async_context(file_index_service())
        client = await stack.enter_async_context(McpToolClient(transport))
        approvals = ApprovalService(audit, timeout_s=approval_timeout_s)
        policy = PolicyLayer(
            client=client,
            catalog=catalog or default_catalog(),
            permissions=PermissionManager(config),
            audit=audit,
            approvals=approvals,
        )
        runtime = AgentRuntime(
            registry=registry,
            queue=queue or get_model_queue(),
            gateway=policy,
            audit=audit,
            limits=config.limits,
            max_tokens=max_tokens,
        )
        yield Router(runtime, stop=StopControl(audit, approvals), approvals=approvals)
