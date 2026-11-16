"""Step 6: capability catalogue."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from integrations.mcp.tool_schemas import ToolManifest, ToolSpec
from integrations.registry.capability_catalog import CapabilityCatalog, default_catalog


class _In(BaseModel):
    pass


class _Out(BaseModel):
    ok: bool = True


def _spec(name: str, status: str = "approved") -> ToolSpec:
    manifest = ToolManifest(
        name=name,
        description="d",
        route="pc",
        permission_tier="T0",
        approval_required=False,
        side_effects="none",
        status=status,
    )
    return ToolSpec(manifest=manifest, input_model=_In, output_model=_Out, handler=lambda _: _Out())


def test_register_and_get() -> None:
    cat = CapabilityCatalog()
    cat.register(_spec("a"))
    assert cat.get("a").manifest.name == "a"
    assert cat.get("missing") is None


def test_duplicate_name_rejected() -> None:
    cat = CapabilityCatalog()
    cat.register(_spec("a"))
    with pytest.raises(ValueError, match="already registered"):
        cat.register(_spec("a"))


def test_only_approved_tools_are_callable() -> None:
    cat = CapabilityCatalog()
    cat.register(_spec("ok"))
    cat.register(_spec("q", status="quarantined"))
    cat.register(_spec("off", status="disabled"))
    assert [s.manifest.name for s in cat.callable_tools()] == ["ok"]
    assert {s.manifest.name for s in cat.all_tools()} == {"ok", "q", "off"}


def test_default_catalog_has_the_test_tool() -> None:
    spec = default_catalog().get("get_system_time")
    assert spec is not None
    assert spec.manifest.permission_tier == "T0"
    assert spec.manifest.side_effects == "none"
    assert spec.manifest.status == "approved"
