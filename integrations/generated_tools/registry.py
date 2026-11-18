"""Generated tools as catalogue entries (build step 13).

`GeneratedToolSource` is asked by the capability catalogue every time it looks a tool up, so a
change on disk or an owner decision takes effect at once and in both processes (the gateway
and the tool server) without a restart. Each lookup reads the three files once; the status
and the code that would run both come from those same bytes (no check-then-use gap).

A generated tool is always T1 and read-only (`manifest.GENERATED_TIER`). It joins the
catalogue under its own name unless a hand-written tool already has that name (reserved).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from integrations.generated_tools.executor import HostFetcher, _host_fetch, run_generated
from integrations.generated_tools.manifest import ADAPTER_FILE, generated_root
from integrations.generated_tools.store import GeneratedStore, ToolFiles, list_tool_names, read_tool
from integrations.mcp.tool_schemas import ToolSpec


class GeneratedToolSource:
    def __init__(
        self,
        reserved: frozenset[str] = frozenset(),
        root: Path | None = None,
        store: GeneratedStore | None = None,
        fetch: HostFetcher = _host_fetch,
    ) -> None:
        self.root = root or generated_root()
        self.store = store or GeneratedStore(root=self.root)
        self.reserved = reserved
        self._fetch = fetch

    def get(self, name: str) -> ToolSpec | None:
        if name in self.reserved:
            return None
        tool = read_tool(name, self.root)
        return self._spec(tool) if tool is not None else None

    def all_specs(self) -> list[ToolSpec]:
        specs = (self.get(name) for name in list_tool_names(self.root))
        return [s for s in specs if s is not None]

    def granted_to(self, agent_id: str) -> frozenset[str]:
        """Approved generated tools the owner assigned to this agent."""
        names: set[str] = set()
        for name in list_tool_names(self.root):
            if name in self.reserved:
                continue
            tool = read_tool(name, self.root)
            if tool is not None and agent_id in self.store.granted_agents(tool):
                names.add(name)
        return frozenset(names)

    def _spec(self, tool: ToolFiles) -> ToolSpec | None:
        manifest = tool.manifest
        if manifest is None:  # unusable: shown to the owner by the review screen, not the catalogue
            return None
        status, _reason = self.store.status_of(tool)
        source = tool.text(ADAPTER_FILE)
        hosts = frozenset(manifest.permissions.network)
        timeout = manifest.timeout_s
        fetch = self._fetch
        out_model = manifest.output_model()

        def handler(args: BaseModel) -> BaseModel:
            if status != "approved":  # defence in depth: the gateway and server check already
                raise PermissionError(f"tool '{manifest.name}' is {status}")
            result = run_generated(
                source, args.model_dump(), hosts=hosts, timeout_s=timeout, fetch=fetch
            )
            return out_model.model_validate(result)

        return ToolSpec(
            manifest=manifest.tool_manifest(status),
            input_model=manifest.input_model(),
            output_model=out_model,
            handler=handler,
        )


def default_source(reserved: frozenset[str]) -> GeneratedToolSource:
    return GeneratedToolSource(reserved=reserved)


def granted_tool_names(agent_id: str, source: GeneratedToolSource | None = None) -> frozenset[str]:
    """Names to add to an agent's card allowlist (owner-assigned, approved, unchanged tools)."""
    try:
        src = source or GeneratedToolSource(reserved=_static_names())
        return src.granted_to(agent_id)
    except Exception:  # noqa: BLE001 - a broken tool folder must never stop an agent
        return frozenset()


_static_cache: frozenset[str] | None = None


def _static_names() -> frozenset[str]:
    """Names of the hand-written tools (a generated tool may not take one of them)."""
    global _static_cache
    if _static_cache is None:
        from integrations.registry.capability_catalog import default_catalog

        _static_cache = frozenset(
            s.name for s in default_catalog(include_generated=False).all_tools()
        )
    return _static_cache


def reserved_names() -> frozenset[str]:
    return _static_names()


__all__ = [
    "GeneratedToolSource",
    "default_source",
    "granted_tool_names",
    "reserved_names",
]
