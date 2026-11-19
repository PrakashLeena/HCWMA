"""Verify check 8: every registered tool that takes a path rejects a protected path and a
path outside the approved folders. Path-taking tools are found from the input models, so a
new one is caught here (the expected list must be updated on purpose)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import anyio
import pytest

from integrations.adapters.browser.web_tools import WebToolDeps, web_tool_specs
from integrations.adapters.desktop.ui_interface import UiGuard
from integrations.adapters.desktop.ui_tools import UiToolDeps, ui_tool_specs
from integrations.adapters.desktop.whatsapp_tools import WhatsAppToolDeps, whatsapp_tool_specs
from integrations.adapters.local_data.memory_tools import MemoryToolDeps, memory_tool_specs
from integrations.adapters.local_data.planner_store import PlannerStore
from integrations.adapters.local_data.planner_tools import PlannerDeps, planner_tool_specs
from integrations.adapters.local_data.tool_builder_tools import (
    ToolBuilderDeps,
    tool_builder_specs,
)
from integrations.adapters.official_api.gmail_tools import GmailToolDeps, gmail_tool_specs
from integrations.adapters.pc.code_tools import CodeToolDeps, code_tool_specs
from integrations.adapters.pc.document_tools import DocumentToolDeps, document_tool_specs
from integrations.adapters.pc.file_tools import FileToolDeps, file_tool_specs
from integrations.adapters.pc.file_write_tools import file_write_tool_specs
from integrations.adapters.pc.image_tools import ImageToolDeps, image_tool_specs
from integrations.adapters.pc.recycle_bin import RecycledItem
from integrations.adapters.pc.system_time import get_system_time_spec
from integrations.mcp.server import execute
from integrations.mcp.tool_schemas import CALLER_KEY, CallerContext
from integrations.registry.capability_catalog import CapabilityCatalog, default_catalog
from security.permissions.path_check import PathPolicy
from tests.pc_layout import Layout, make_layout

PATH_FIELD = re.compile(r"path|folder|dir|cwd|root|source|destination")

# (tool, field) pairs expected to take a path. A new path-taking tool must be added here
# and to `_args` below.
EXPECTED = {
    ("apply_filter", "paths"),
    ("apply_patch", "path"),
    ("convert_format", "paths"),
    ("crop_image", "paths"),
    ("delete_file", "path"),
    ("get_disk_usage", "folder"),
    ("image_info", "path"),
    ("move_file", "destination"),
    ("move_file", "source"),
    ("read_document", "path"),
    ("read_file", "path"),
    ("read_logs", "path"),
    ("reindex_folder", "folder"),
    ("resize_image", "paths"),
    ("run_in_sandbox", "cwd"),
    ("run_tests", "path"),
    ("search_files", "roots"),
    ("write_file", "path"),
}


class _NoBin:
    def list_items(self, drives: Sequence[Path]) -> list[RecycledItem]:
        return []


def _catalog(pc: Layout) -> CapabilityCatalog:
    deps = FileToolDeps(index=pc.index, policy=pc.policy, recycle_bin=_NoBin())  # type: ignore[arg-type]
    mem = MemoryToolDeps(
        service=lambda: (_ for _ in ()).throw(AssertionError("memory service not expected")),
        index=lambda: pc.index,
        policy=lambda: pc.policy,
    )
    catalog = CapabilityCatalog()
    for spec in (
        get_system_time_spec(),
        *file_tool_specs(deps, status="approved"),
        *file_write_tool_specs(deps, status="approved"),
        *code_tool_specs(CodeToolDeps(pc.policy), status="approved"),
        *image_tool_specs(ImageToolDeps(pc.policy), status="approved"),
        *memory_tool_specs(mem, status="approved"),
        *web_tool_specs(WebToolDeps(), status="approved"),
        *ui_tool_specs(UiToolDeps(None, UiGuard()), status="approved"),  # type: ignore[arg-type]
        *tool_builder_specs(ToolBuilderDeps(), status="approved"),
        *gmail_tool_specs(GmailToolDeps(), status="approved"),
        *whatsapp_tool_specs(WhatsAppToolDeps(None), status="approved"),  # type: ignore[arg-type]
        *planner_tool_specs(PlannerDeps(PlannerStore(pc.tmp / "planner.sqlite3")), "approved"),
        *document_tool_specs(DocumentToolDeps(pc.policy, pc.index), status="approved"),
    ):
        catalog.register(spec)
    return catalog


def _path_fields(catalog: CapabilityCatalog) -> set[tuple[str, str]]:
    return {
        (spec.name, field)
        for spec in catalog.all_tools()
        for field in spec.input_model.model_fields
        if PATH_FIELD.search(field)
    }


def _args(pc: Layout, tool: str, field: str, bad: str) -> dict[str, Any]:
    ok_file = str(pc.docs / "notes" / "meeting notes.txt")
    base: dict[str, Any] = {
        "apply_filter": {"paths": [ok_file], "filter": "grayscale"},
        "apply_patch": {"path": ok_file, "edits": [{"old": "a", "new": "b"}]},
        "convert_format": {"paths": [ok_file], "format": "png"},
        "crop_image": {"paths": [ok_file]},
        "delete_file": {"path": ok_file},
        "get_disk_usage": {},
        "image_info": {"path": ok_file},
        "move_file": {"source": ok_file, "destination": str(pc.docs / "notes" / "moved.txt")},
        "read_document": {"path": ok_file},
        "read_file": {"path": ok_file},
        "read_logs": {},
        "reindex_folder": {},
        "resize_image": {"paths": [ok_file], "width": 10},
        "run_in_sandbox": {"command": "python a.py"},
        "run_tests": {},
        "search_files": {"query": "report"},
        "write_file": {"path": "a.py", "content": "x"},
    }[tool]
    base[field] = [bad] if field in ("roots", "paths") else bad
    if tool == "reindex_folder":
        base[CALLER_KEY] = CallerContext(agent_id="a", task_id="t").model_dump(mode="json")
    return base


def test_path_taking_tools_are_found_and_default_catalog_is_covered(tmp_path: Path) -> None:
    pc = make_layout(tmp_path)
    assert _path_fields(_catalog(pc)) == EXPECTED
    assert _path_fields(default_catalog()) == EXPECTED  # what really ships
    assert {s.name for s in _catalog(pc).all_tools()} == {
        s.name for s in default_catalog().all_tools()
    }


@pytest.mark.parametrize(("tool", "field"), sorted(EXPECTED))
def test_rejects_protected_and_outside_paths(tmp_path: Path, tool: str, field: str) -> None:
    pc = make_layout(tmp_path)
    catalog = _catalog(pc)
    cases = {
        "protected ~/.ssh": (str(pc.home / ".ssh" / "id_rsa"), "protected path"),
        "protected folder": (str(pc.secrets / "annek-passwords.txt"), "protected path"),
        "outside approved": (
            str(pc.outside / "annek-private.txt"),
            "not inside an approved folder|outside the HCWMA workspace",
        ),
    }
    for label, (bad, expected) in cases.items():
        result = anyio.run(execute, catalog, tool, _args(pc, tool, field, bad))
        assert not result.ok, f"{tool}.{field} accepted {label}"
        assert re.search(expected, result.error or ""), (
            f"{tool}.{field} ({label}) failed for another reason: {result.error}"
        )
    assert (pc.outside / "annek-private.txt").read_text() == "not approved"
    assert (pc.home / ".ssh" / "id_ed25519").read_text() == "private key"


def test_real_config_refuses_the_real_ssh_folder() -> None:
    policy = PathPolicy.from_config()
    for access in ("read", "write", "modify"):
        with pytest.raises(PermissionError, match="protected"):
            policy.check(Path.home() / ".ssh" / "id_rsa", access)  # type: ignore[arg-type]
