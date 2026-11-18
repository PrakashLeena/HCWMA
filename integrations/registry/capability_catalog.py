"""Capability catalogue: every tool HCWMA knows about (master document 6.4).

Only tools with status `approved` are exposed over MCP or callable. Quarantined and
disabled tools stay registered so the owner can review them.
"""

from __future__ import annotations

from integrations.mcp.tool_schemas import ToolSpec


class CapabilityCatalog:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool '{spec.name}' is already registered")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def all_tools(self) -> list[ToolSpec]:
        return sorted(self._tools.values(), key=lambda s: s.name)

    def callable_tools(self) -> list[ToolSpec]:
        return [s for s in self.all_tools() if s.manifest.status == "approved"]


def default_catalog() -> CapabilityCatalog:
    """The hand-written tools shipped with HCWMA. Later steps add theirs here."""
    from integrations.adapters.local_data.memory_tools import default_memory_tool_specs
    from integrations.adapters.pc.code_tools import default_code_tool_specs
    from integrations.adapters.pc.file_tools import default_file_tool_specs
    from integrations.adapters.pc.file_write_tools import default_file_write_tool_specs
    from integrations.adapters.pc.image_tools import default_image_tool_specs
    from integrations.adapters.pc.system_time import get_system_time_spec

    catalog = CapabilityCatalog()
    catalog.register(get_system_time_spec())
    for spec in default_file_tool_specs():  # step 8: read-only file tools
        catalog.register(spec)
    for spec in default_file_write_tool_specs():  # step 9: restore / move / delete (T3, T4)
        catalog.register(spec)
    for spec in default_memory_tool_specs():  # step 10: the Librarian's memory tools
        catalog.register(spec)
    for spec in default_code_tool_specs():  # step 11: coding workspace tools (quarantined)
        catalog.register(spec)
    for spec in default_image_tool_specs():  # step 12: image editing (copies only)
        catalog.register(spec)
    return catalog
