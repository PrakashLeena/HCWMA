"""Wire the real Router: cards → registry, MCP gateway → policy layer, shared model queue."""

from __future__ import annotations

import logging
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
from memory.service import MemoryService
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


log = logging.getLogger(__name__)
MAINTENANCE_INTERVAL_S = 24 * 3600


@asynccontextmanager
async def memory_maintenance(
    memory: MemoryService, interval_s: float = MAINTENANCE_INTERVAL_S
) -> AsyncIterator[None]:
    """The daily memory job (memory policy 6 and 8): remove expired entries, prune the
    audit log, back up when due. Runs once at start, then every `interval_s`. Logged."""

    async def loop() -> None:
        while True:
            try:
                await anyio.to_thread.run_sync(memory.maintain)
            except Exception:  # noqa: BLE001 - try again next time; never stop the app
                log.exception("memory maintenance failed")
            await anyio.sleep(interval_s)

    async with anyio.create_task_group() as tg:
        tg.start_soon(loop)
        try:
            yield
        finally:
            tg.cancel_scope.cancel()


@asynccontextmanager
async def open_router(
    queue: ModelQueue | None = None,
    audit: AuditLogger | None = None,
    max_tokens: int = 512,
    registry: AgentRegistry | None = None,
    catalog: CapabilityCatalog | None = None,
    keep_file_index_fresh: bool = False,
    approval_timeout_s: float = DEFAULT_APPROVAL_TIMEOUT_S,
    memory: MemoryService | None = None,
    with_memory: bool = True,
    run_memory_maintenance: bool = False,
) -> AsyncIterator[Router]:
    """Start the local MCP server (stdio child process) and yield a ready Router.

    `catalog`: use these tools on an in-process MCP server instead (demos and tests).
    `keep_file_index_fresh`: also run the PC file index scan + watcher (the backend does).
    `memory`/`with_memory`: the memory service (default: the real one in the HCWMA data
    folder); `with_memory=False` runs without memory.
    `run_memory_maintenance`: also run the daily clean-up and backup job (the backend does).
    """
    config = load_permissions_config()
    registry = registry or AgentRegistry.from_dir()
    audit = audit or get_audit_logger()
    if memory is None and with_memory:
        memory = MemoryService.from_config(audit=audit)
    transport = Client(build_server(catalog)) if catalog else Client(local_server_params())
    async with AsyncExitStack() as stack:
        if keep_file_index_fresh:
            await stack.enter_async_context(file_index_service())
        if run_memory_maintenance and memory is not None:
            await stack.enter_async_context(memory_maintenance(memory))
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
            memory=memory,
        )
        yield Router(
            runtime, stop=StopControl(audit, approvals), approvals=approvals, memory=memory
        )
