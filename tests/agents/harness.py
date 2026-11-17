"""Test harness for Step 7: real registry, runtime, policy layer and MCP server (in process),
with a scripted model in place of Qwen."""

from __future__ import annotations

import json
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from mcp import Client
from pydantic import BaseModel

from agents.registry.agent_registry import AgentRegistry
from agents.router.router import Router
from agents.runtime.agent_runtime import AgentRuntime
from agents.runtime.model_queue import ModelQueue
from agents.runtime.policy import PolicyLayer
from agents.runtime.stop import StopControl
from integrations.mcp.client import McpToolClient
from integrations.mcp.server import build_server
from integrations.mcp.tool_schemas import ToolManifest, ToolSpec
from integrations.registry.capability_catalog import CapabilityCatalog, default_catalog
from memory.service import MemoryService
from model.serving import GenerateResult, Message, ToolCall
from security.approvals.approval_service import ApprovalService, PendingApproval
from security.audit.audit_logger import AuditLogger
from security.permissions.permission_manager import PermissionManager, load_permissions_config

Script = Callable[[list[Message], dict[str, Any]], GenerateResult]


@dataclass
class Call:
    messages: list[Message]
    kwargs: dict[str, Any]

    @property
    def is_router(self) -> bool:
        return self.kwargs.get("response_format") is not None


@dataclass
class ScriptedModel:
    """Stands in for Qwen. `router` answers routing calls, `agent` answers agent calls."""

    router: Script | None = None
    agent: Script | None = None
    calls: list[Call] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def generate(self, messages: list[Message], **kwargs: Any) -> GenerateResult:
        with self._lock:
            self.calls.append(Call(list(messages), kwargs))
        script = self.router if kwargs.get("response_format") is not None else self.agent
        if script is None:
            raise AssertionError("unexpected model call")
        return script(messages, kwargs)

    def agent_calls(self) -> list[Call]:
        return [c for c in self.calls if not c.is_router]


def route(action: str = "delegate", confidence: float = 0.9, **fields: Any) -> Script:
    payload = {"action": action, "confidence": confidence, **fields}
    return lambda _m, _k: GenerateResult(text=json.dumps(payload))


def say(text: str) -> Script:
    return lambda _m, _k: GenerateResult(text=text)


def tool_calls(*names: str) -> GenerateResult:
    return GenerateResult(text="", tool_calls=[ToolCall(name=n) for n in names])


class NoArgs(BaseModel):
    pass


class Note(BaseModel):
    text: str


def add_tool(
    catalog: CapabilityCatalog,
    name: str,
    *,
    tier: str = "T0",
    returns: str = "ok",
    calls: list[str] | None = None,
) -> None:
    high = tier in ("T3", "T4")
    manifest = ToolManifest(
        name=name,
        description=f"test tool {name}",
        route="pc",
        permission_tier=tier,
        approval_required=high,
        side_effects="writes" if high else "none",
        status="approved",
    )

    def handler(_: NoArgs) -> Note:
        if calls is not None:
            calls.append(name)
        return Note(text=returns)

    catalog.register(
        ToolSpec(manifest=manifest, input_model=NoArgs, output_model=Note, handler=handler)
    )


REPO_AGENTS = Path(__file__).resolve().parents[2] / "agents"


def write_card(
    agents_dir: Path, card_id: str, *, prompt: str = "You are a test agent.", **fields: Any
) -> None:
    import yaml

    data: dict[str, Any] = {
        "id": card_id,
        "name": card_id.title(),
        "status": "enabled",
        "description": f"{card_id} test agent",
        "handles": [f"{card_id} things"],
        "tools": [],
        "permission_tier": "T0",
        "system_prompt": f"prompts/{card_id}.md",
    }
    data.update(fields)
    (agents_dir / "cards").mkdir(parents=True, exist_ok=True)
    (agents_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (agents_dir / "cards" / f"{card_id}.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    (agents_dir / "prompts" / f"{card_id}.md").write_text(prompt, encoding="utf-8")


def copy_real_agents(dest: Path) -> Path:
    """The shipped router + echo cards and prompts, in a folder a test may add cards to."""
    for sub in ("cards", "prompts"):
        (dest / sub).mkdir(parents=True, exist_ok=True)
        for f in (REPO_AGENTS / sub).iterdir():
            if f.suffix in (".yaml", ".md"):
                (dest / sub / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


@dataclass
class Stack:
    router: Router
    runtime: AgentRuntime
    audit: AuditLogger
    model: ScriptedModel
    registry: AgentRegistry
    policy: PolicyLayer
    approvals: ApprovalService | None
    stop: StopControl


@asynccontextmanager
async def stack(
    tmp_path: Path,
    model: ScriptedModel,
    *,
    agents_dir: Path | None = None,
    catalog: CapabilityCatalog | None = None,
    approval_timeout_s: float | None = None,
    memory: MemoryService | None = None,
) -> AsyncIterator[Stack]:
    """`approval_timeout_s`: add the Step 9 approval service and stop control (None = the
    stack has no way to show a prompt, so nothing that needs approval can run)."""
    registry = AgentRegistry.from_dir((agents_dir or REPO_AGENTS) / "cards")
    catalog = catalog or default_catalog()
    audit = AuditLogger(tmp_path / "audit.jsonl")
    config = load_permissions_config()
    approvals = ApprovalService(audit, approval_timeout_s) if approval_timeout_s else None
    async with McpToolClient(Client(build_server(catalog))) as client:
        policy = PolicyLayer(
            client=client,
            catalog=catalog,
            permissions=PermissionManager(config),
            audit=audit,
            approvals=approvals,
        )
        runtime = AgentRuntime(
            registry=registry,
            queue=ModelQueue(model.generate),
            gateway=policy,
            audit=audit,
            limits=config.limits,
            memory=memory,
        )
        stop = StopControl(audit, approvals)
        router = Router(runtime, stop=stop, approvals=approvals, memory=memory)
        yield Stack(router, runtime, audit, model, registry, policy, approvals, stop)


async def answer_next_prompt(
    approvals: ApprovalService, approve: bool, seen: list[PendingApproval] | None = None
) -> None:
    """Plays the owner: waits for one approval prompt and answers it."""
    while not approvals.pending():
        await anyio.sleep(0.01)
    prompt = approvals.pending()[0]
    if seen is not None:
        seen.append(prompt)
    assert approvals.decide(prompt.id, approve)
