"""Capability catalogue: every tool HCWMA knows about (master document 6.4).

Only tools with status `approved` are exposed over MCP or callable. Quarantined and
disabled tools stay registered so the owner can review them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from integrations.mcp.tool_schemas import ToolSpec

if TYPE_CHECKING:
    from integrations.generated_tools.registry import GeneratedToolSource


class CapabilityCatalog:
    def __init__(self, generated: GeneratedToolSource | None = None) -> None:
        self._tools: dict[str, ToolSpec] = {}
        # Tools written by the Tool Builder (step 13), looked up fresh every time so an owner
        # decision or an edited file takes effect at once. Hand-written tools win a name clash.
        self._generated = generated

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool '{spec.name}' is already registered")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        spec = self._tools.get(name)
        if spec is None and self._generated is not None:
            spec = self._generated.get(name)
        return spec

    def all_tools(self) -> list[ToolSpec]:
        specs = dict(self._tools)
        if self._generated is not None:
            for spec in self._generated.all_specs():
                specs.setdefault(spec.name, spec)
        return sorted(specs.values(), key=lambda s: s.name)

    def callable_tools(self) -> list[ToolSpec]:
        return [s for s in self.all_tools() if s.manifest.status == "approved"]


def default_catalog(include_generated: bool = True) -> CapabilityCatalog:
    """The hand-written tools shipped with HCWMA, plus (step 13) the tools the Tool Builder
    wrote. Generated tools are quarantined until the owner approves their exact files."""
    from integrations.adapters.browser.web_tools import default_web_tool_specs
    from integrations.adapters.desktop.ui_tools import default_ui_tool_specs
    from integrations.adapters.desktop.whatsapp_tools import default_whatsapp_tool_specs
    from integrations.adapters.local_data.memory_tools import default_memory_tool_specs
    from integrations.adapters.local_data.planner_tools import default_planner_tool_specs
    from integrations.adapters.local_data.tool_builder_tools import default_tool_builder_specs
    from integrations.adapters.official_api.gmail_tools import default_gmail_tool_specs
    from integrations.adapters.pc.code_tools import default_code_tool_specs
    from integrations.adapters.pc.document_tools import default_document_tool_specs
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
    for spec in default_web_tool_specs():  # step 12: read-only web pages (quarantined)
        catalog.register(spec)
    for spec in default_ui_tool_specs():  # step 12: desktop apps, T3 actions (quarantined)
        catalog.register(spec)
    for spec in default_tool_builder_specs():  # step 13: the Tool Builder's own tools
        catalog.register(spec)
    for spec in default_gmail_tool_specs():  # step 15: Gmail search/read/draft/send (quarantined)
        catalog.register(spec)
    for spec in default_whatsapp_tool_specs():  # step 15: WhatsApp Desktop list/read/send
        catalog.register(spec)
    for spec in default_planner_tool_specs():  # step 15: reminders, to-dos, deadlines
        catalog.register(spec)
    for spec in default_document_tool_specs():  # step 15: read documents, create new ones
        catalog.register(spec)
    if include_generated:
        from integrations.generated_tools.registry import GeneratedToolSource

        reserved = frozenset(s.name for s in catalog.all_tools())
        catalog._generated = GeneratedToolSource(reserved=reserved)  # noqa: SLF001
    return catalog
