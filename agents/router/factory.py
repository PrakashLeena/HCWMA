"""Wire the real Router: cards → registry, MCP gateway → policy layer, shared model queue."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp import Client

from agents.registry.agent_registry import AgentRegistry
from agents.router.router import Router
from agents.runtime.agent_runtime import AgentRuntime
from agents.runtime.model_queue import ModelQueue, get_model_queue
from agents.runtime.policy import PolicyLayer
from integrations.mcp.client import McpToolClient, local_server_params
from integrations.registry.capability_catalog import default_catalog
from security.audit.audit_logger import AuditLogger, get_audit_logger
from security.permissions.permission_manager import PermissionManager, load_permissions_config


@asynccontextmanager
async def open_router(
    queue: ModelQueue | None = None,
    audit: AuditLogger | None = None,
    max_tokens: int = 512,
    registry: AgentRegistry | None = None,
) -> AsyncIterator[Router]:
    """Start the local MCP server (stdio child process) and yield a ready Router."""
    config = load_permissions_config()
    registry = registry or AgentRegistry.from_dir()
    audit = audit or get_audit_logger()
    async with McpToolClient(Client(local_server_params())) as client:
        policy = PolicyLayer(
            client=client,
            catalog=default_catalog(),
            permissions=PermissionManager(config),
            audit=audit,
        )
        runtime = AgentRuntime(
            registry=registry,
            queue=queue or get_model_queue(),
            gateway=policy,
            audit=audit,
            limits=config.limits,
            max_tokens=max_tokens,
        )
        yield Router(runtime)
