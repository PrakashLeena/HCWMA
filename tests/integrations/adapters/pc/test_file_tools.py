"""Step 8: the four read-only file tools (add-tool skill test groups), on temp folders only.

Not applicable: credentials (no secrets, no network). "Approval-required action blocked":
all four are read-only T0/T1 with no approval; the generic block is tested in
tests/agents/runtime/test_policy.py, and the manifest test below pins them read-only.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from pydantic import ValidationError

from agents.runtime.policy import PolicyLayer
from integrations.adapters.pc import file_tools
from integrations.adapters.pc.file_tools import (
    NO_FOLDERS_NOTE,
    CheckRecycleBinInput,
    FileToolDeps,
    GetDiskUsageInput,
    ListRecentFilesInput,
    SearchFilesInput,
    file_tool_specs,
)
from integrations.adapters.pc.recycle_bin import RecycleBinUnavailable, RecycledItem
from integrations.mcp.client import McpToolClient
from integrations.mcp.server import build_server, execute
from integrations.mcp.tool_schemas import ToolSpec
from integrations.registry.capability_catalog import CapabilityCatalog, default_catalog
from memory.pc_index.index import FileEntry
from memory.pc_index.scanner import scan
from security.audit.audit_logger import AuditLogger
from security.permissions.path_check import PathPolicy
from security.permissions.permission_manager import (
    AgentGrant,
    PermissionManager,
    load_permissions_config,
)
from tests.pc_layout import Layout, make_layout, write

TOOL_NAMES = ["check_recycle_bin", "get_disk_usage", "list_recent_files", "search_files"]


class FakeBin:
    def __init__(self, items: Sequence[RecycledItem] = (), unavailable: bool = False) -> None:
        self.items = list(items)
        self.unavailable = unavailable
        self.drives: list[Path] = []

    def list_items(self, drives: Sequence[Path]) -> list[RecycledItem]:
        self.drives = list(drives)
        if self.unavailable:
            raise RecycleBinUnavailable("reading the Recycle Bin is not supported on testos")
        return self.items


@pytest.fixture
def pc(tmp_path: Path) -> Layout:
    layout = make_layout(tmp_path)
    scan(layout.index, layout.policy)
    return layout


def _specs(
    pc: Layout, bin_: FakeBin | None = None, status: str = "approved"
) -> dict[str, ToolSpec]:
    deps = FileToolDeps(index=pc.index, policy=pc.policy, recycle_bin=bin_ or FakeBin())
    return {s.name: s for s in file_tool_specs(deps, status=status)}  # type: ignore[arg-type]


def _call(pc: Layout, tool: str, bin_: FakeBin | None = None, **args: Any) -> Any:
    spec = _specs(pc, bin_)[tool]
    return spec.output_model.model_validate(spec.handler(spec.input_model.model_validate(args)))


def _names(result: Any) -> list[str]:
    return [Path(hit.path).name for hit in result.results]


def _deleted(pc: Layout, path: Path, days_ago: int = 1, size: int = 10) -> RecycledItem:
    return RecycledItem(
        item_id=f"X:|$R{path.stem[:6]}{path.suffix}",
        original_path=str(path),
        deleted_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        size_bytes=size,
    )


# ---------- search_files ----------


def test_search_finds_a_file_by_part_of_its_name(pc: Layout) -> None:
    out = _call(pc, "search_files", query="annek proposal")
    assert _names(out) == ["annek-proposal_v2.docx"]
    assert out.match == "all_words" and out.count == 1 and not out.more_available
    hit = out.results[0]
    assert hit.path == str(pc.docs / "projects" / "annek-proposal_v2.docx")
    assert hit.size_bytes == 8 and len(hit.modified) == 16
    assert out.searched_folders == [str(pc.docs), str(pc.workspace)]


def test_search_falls_back_to_some_words_and_says_so(pc: Layout) -> None:
    out = _call(pc, "search_files", query="my missing annek file")
    assert _names(out) == ["annek-proposal_v2.docx"]
    assert out.match == "some_words" and "every word" in out.note


def test_search_reports_not_found_honestly(pc: Layout) -> None:
    out = _call(pc, "search_files", query="quarterly-forecast")
    assert out.results == [] and out.count == 0 and out.match == "none"
    assert "Nothing in the index matches" in out.note
    # It names every searched folder, so the owner can tell what was never looked at.
    assert str(pc.docs) in out.note and str(pc.workspace) in out.note


def test_search_never_returns_protected_or_unapproved_files(pc: Layout) -> None:
    for query in ("annek", "passwords", "id_ed25519", "private"):
        names = _names(_call(pc, "search_files", query=query, limit=50))
        assert "annek-passwords.txt" not in names
        assert "id_ed25519" not in names
        assert "annek-private.txt" not in names


def test_results_are_rechecked_even_if_the_index_holds_a_forbidden_row(pc: Layout) -> None:
    """Defence in depth: a stale or tampered index row is filtered by the path check."""
    leaked = [pc.secrets / "annek-passwords.txt", pc.outside / "annek-private.txt"]
    pc.index.upsert(
        FileEntry(path=str(p), size=1, mtime=time.time(), root=str(pc.docs)) for p in leaked
    )
    assert _names(_call(pc, "search_files", query="annek")) == ["annek-proposal_v2.docx"]
    assert "annek-passwords.txt" not in _names(_call(pc, "list_recent_files", days=1))
    usage = _call(pc, "get_disk_usage", largest=20)
    assert "annek-passwords.txt" not in [Path(h.path).name for h in usage.largest_files]


def test_search_drops_files_deleted_since_the_last_scan(pc: Layout) -> None:
    (pc.docs / "projects" / "annek-proposal_v2.docx").unlink()
    assert _call(pc, "search_files", query="annek").match == "none"


def test_search_roots_extension_and_limit(pc: Layout) -> None:
    assert _names(_call(pc, "search_files", query="report", roots=[str(pc.workspace)])) == [
        "draft-report.md"
    ]
    assert _call(pc, "search_files", query="report", roots=[str(pc.docs)]).match == "none"
    assert _names(_call(pc, "search_files", query=".xlsx")) == ["budget 2026.xlsx"]
    assert _names(_call(pc, "search_files", query="projects", extension="docx")) == [
        "annek-proposal_v2.docx"
    ]
    limited = _call(pc, "search_files", query="projects", limit=1)
    assert limited.count == 1 and limited.more_available


def test_search_refuses_a_root_outside_policy(pc: Layout) -> None:
    for bad in (pc.outside, pc.secrets, pc.home / ".ssh"):
        result = anyio.run(
            execute, _catalog(pc), "search_files", {"query": "annek", "roots": [str(bad)]}
        )
        assert not result.ok and result.error_code == "tool_error"
        assert "PathDenied" in result.error


def test_invalid_arguments_rejected() -> None:
    for bad in (
        {},
        {"query": ""},
        {"query": "x", "limit": 0},
        {"query": "x", "limit": 500},
        {"query": "x", "extension": "../etc"},
        {"query": "x", "path": "C:/Windows"},
    ):
        with pytest.raises(ValidationError):
            SearchFilesInput.model_validate(bad)
    with pytest.raises(ValidationError):
        ListRecentFilesInput.model_validate({"days": 0})
    with pytest.raises(ValidationError):
        CheckRecycleBinInput.model_validate({"query": "x", "restore": True})
    with pytest.raises(ValidationError):
        GetDiskUsageInput.model_validate({"largest": 999})


def test_nothing_approved_is_reported_not_guessed(pc: Layout) -> None:
    policy = PathPolicy(
        repo_root=pc.tmp / "empty",
        home=pc.home,
        workspace="workspace",
        approved_folders=[],
        protected_paths=[],
        blocked_folders=[],
    )
    deps = FileToolDeps(index=pc.index, policy=policy, recycle_bin=FakeBin())
    specs = {s.name: s for s in file_tool_specs(deps, status="approved")}
    out = specs["search_files"].handler(SearchFilesInput(query="annek"))
    assert out.results == [] and out.note == NO_FOLDERS_NOTE and out.searched_folders == []
    assert specs["list_recent_files"].handler(ListRecentFilesInput()).note == NO_FOLDERS_NOTE
    assert specs["check_recycle_bin"].handler(CheckRecycleBinInput()).note == NO_FOLDERS_NOTE


# ---------- list_recent_files ----------


def test_recent_files_newest_first_within_the_window(pc: Layout) -> None:
    now = time.time()
    write(pc.docs / "old.txt", mtime=now - 30 * 86_400)
    write(pc.docs / "yesterday.txt", mtime=now - 86_400)
    write(pc.docs / "today.pdf", mtime=now - 60)
    scan(pc.index, pc.policy)
    out = _call(pc, "list_recent_files", days=2, limit=50)
    names = _names(out)
    assert "old.txt" not in names and "annek-passwords.txt" not in names
    assert names.index("yesterday.txt") == len(names) - 1
    assert _names(_call(pc, "list_recent_files", days=2, extension="pdf")) == ["today.pdf"]
    assert "old.txt" in _names(_call(pc, "list_recent_files", days=60, limit=50))


def test_recent_files_empty_window_says_so(pc: Layout) -> None:
    old = time.time() - 10 * 86_400
    for path, _ in pc.index.sizes(pc.tmp):
        write(Path(path), mtime=old)
    scan(pc.index, pc.policy)
    out = _call(pc, "list_recent_files", days=1)
    assert out.results == [] and "last 1 day" in out.note


# ---------- check_recycle_bin ----------


def test_recycle_bin_lists_matching_deleted_files_newest_first(pc: Layout) -> None:
    bin_ = FakeBin(
        [
            _deleted(pc, pc.docs / "projects" / "thesis-final.docx", days_ago=5, size=77),
            _deleted(pc, pc.docs / "thesis-notes.txt", days_ago=1),
            _deleted(pc, pc.docs / "holiday.jpg", days_ago=2),
        ]
    )
    out = _call(pc, "check_recycle_bin", bin_, query="thesis")
    assert [Path(h.original_path).name for h in out.results] == [
        "thesis-notes.txt",
        "thesis-final.docx",
    ]
    assert out.supported and out.results[1].size_bytes == 77 and out.results[1].item_id
    assert bin_.drives == sorted({Path(pc.docs.anchor), Path(pc.workspace.anchor)})
    assert _call(pc, "check_recycle_bin", bin_, query="").count == 3


def test_recycle_bin_hides_files_deleted_from_protected_or_unapproved_places(pc: Layout) -> None:
    bin_ = FakeBin(
        [
            _deleted(pc, pc.secrets / "thesis-keys.txt"),
            _deleted(pc, pc.home / ".ssh" / "thesis_rsa"),
            _deleted(pc, pc.outside / "thesis-private.txt"),
        ]
    )
    out = _call(pc, "check_recycle_bin", bin_, query="thesis")
    assert out.results == [] and "Nothing matching" in out.note


def test_recycle_bin_unsupported_os_is_reported(pc: Layout) -> None:
    out = _call(pc, "check_recycle_bin", FakeBin(unavailable=True), query="thesis")
    assert out.results == [] and not out.supported and "not supported" in out.note


# ---------- get_disk_usage ----------


def test_disk_usage_reports_drives_folders_and_largest_files(pc: Layout) -> None:
    write(pc.docs / "videos" / "big.mp4", "v" * 5000)
    scan(pc.index, pc.policy)
    out = _call(pc, "get_disk_usage", largest=2)
    assert out.drives and all(
        d.free_gb <= d.total_gb and 0 <= d.used_percent <= 100 for d in out.drives
    )
    by_path = {f.path: f for f in out.folders}
    assert by_path[str(pc.docs)].files == 4 and by_path[str(pc.workspace)].files == 1
    assert out.largest_subfolders[0].path == str(pc.docs / "videos")
    assert Path(out.largest_files[0].path).name == "big.mp4" and len(out.largest_files) == 2
    assert all("secrets" not in f.path for f in out.largest_subfolders)


def test_disk_usage_for_one_folder_and_refusal_outside_policy(pc: Layout) -> None:
    out = _call(pc, "get_disk_usage", folder=str(pc.docs / "projects"), largest=0)
    assert [f.files for f in out.folders] == [2] and out.largest_files == []
    for bad in (pc.outside, pc.secrets):
        result = anyio.run(execute, _catalog(pc), "get_disk_usage", {"folder": str(bad)})
        assert not result.ok and "PathDenied" in result.error


# ---------- manifests, MCP, permissions, audit ----------


def _catalog(pc: Layout, status: str = "approved") -> CapabilityCatalog:
    catalog = CapabilityCatalog()
    for spec in _specs(pc, status=status).values():
        catalog.register(spec)
    return catalog


@asynccontextmanager
async def _gateway(pc: Layout, status: str = "approved") -> AsyncIterator[tuple[Any, Any]]:
    catalog = _catalog(pc, status)
    audit = AuditLogger(pc.tmp / "audit" / "audit.jsonl")
    async with McpToolClient(Client(build_server(catalog))) as client:
        policy = PolicyLayer(
            client=client,
            catalog=catalog,
            permissions=PermissionManager(load_permissions_config()),
            audit=audit,
        )
        yield policy, audit


FILE_AGENT = AgentGrant(agent_id="file_system", tools=frozenset(TOOL_NAMES), max_tier="T1")


def test_manifests_are_read_only_and_low_tier(pc: Layout) -> None:
    specs = _specs(pc)
    assert sorted(specs) == TOOL_NAMES
    for spec in specs.values():
        m = spec.manifest
        assert m.side_effects == "none" and not m.approval_required
        assert m.permissions.network == [] and m.permissions.secrets == []
        assert m.route == "pc"
    assert specs["search_files"].manifest.permission_tier == "T0"
    assert specs["list_recent_files"].manifest.permission_tier == "T0"
    assert specs["check_recycle_bin"].manifest.permission_tier == "T1"
    assert specs["get_disk_usage"].manifest.permission_tier == "T1"


def test_new_tools_are_quarantined_unless_the_owner_approved_them() -> None:
    names = {s.name for s in default_catalog().all_tools()}
    assert set(TOOL_NAMES) <= names
    for name in TOOL_NAMES:
        assert default_catalog().get(name).manifest.status == file_tools.FILE_TOOLS_STATUS


def test_discovered_and_called_through_mcp_with_an_audit_record(pc: Layout) -> None:
    async def run() -> None:
        async with _gateway(pc) as (policy, audit):
            assert sorted(t.name for t in await policy.list_tools_for(FILE_AGENT)) == TOOL_NAMES
            result = await policy.call_tool(
                "task-1", FILE_AGENT, "search_files", {"query": "annek"}
            )
            assert result.ok and result.data["count"] == 1
            for tool in ("list_recent_files", "check_recycle_bin", "get_disk_usage"):
                assert (await policy.call_tool("task-1", FILE_AGENT, tool, {})).ok
            records = audit.read_all()
            assert [r.tool for r in records] == [
                "search_files",
                "list_recent_files",
                "check_recycle_bin",
                "get_disk_usage",
            ]
            assert all(r.outcome == "ok" and r.agent == "file_system" for r in records)
            assert records[0].args == {"query": "annek"} and records[0].task_id == "task-1"

    anyio.run(run)


def test_agents_without_the_tool_or_the_tier_are_refused(pc: Layout) -> None:
    async def run() -> None:
        async with _gateway(pc) as (policy, audit):
            echo = AgentGrant(agent_id="echo", tools=frozenset({"get_system_time"}), max_tier="T0")
            denied = await policy.call_tool("t", echo, "search_files", {"query": "annek"})
            assert not denied.ok and denied.error_code == "denied"
            low = AgentGrant(agent_id="low", tools=frozenset(TOOL_NAMES), max_tier="T0")
            assert (await policy.call_tool("t", low, "search_files", {"query": "annek"})).ok
            too_high = await policy.call_tool("t", low, "check_recycle_bin", {})
            assert too_high.error_code == "denied" and "above agent max tier" in too_high.error
            assert [r.outcome for r in audit.read_all()] == ["denied", "ok", "denied"]

    anyio.run(run)


def test_quarantined_tools_are_not_listed_or_callable(pc: Layout) -> None:
    async def run() -> None:
        async with _gateway(pc, status="quarantined") as (policy, _):
            assert await policy.list_tools_for(FILE_AGENT) == []
            result = await policy.call_tool("t", FILE_AGENT, "search_files", {"query": "annek"})
            assert result.error_code == "denied" and "quarantined" in result.error

    anyio.run(run)


def test_bad_arguments_and_a_refused_root_are_audited_as_failures(pc: Layout) -> None:
    async def run() -> None:
        async with _gateway(pc) as (policy, audit):
            bad = await policy.call_tool(
                "t", FILE_AGENT, "search_files", {"query": "x", "limit": 0}
            )
            assert bad.error_code == "invalid_arguments"
            refused = await policy.call_tool(
                "t", FILE_AGENT, "search_files", {"query": "annek", "roots": [str(pc.secrets)]}
            )
            assert refused.error_code == "tool_error" and "protected path" in refused.error
            assert [r.outcome for r in audit.read_all()] == ["invalid_arguments", "tool_error"]

    anyio.run(run)


def test_a_slow_tool_times_out(pc: Layout, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _specs(pc)["search_files"]

    def slow(_: Any) -> Any:
        time.sleep(1.0)
        raise AssertionError("should have timed out")

    slow_spec = ToolSpec(
        manifest=spec.manifest.model_copy(update={"timeout_s": 0.05}),
        input_model=spec.input_model,
        output_model=spec.output_model,
        handler=slow,
    )
    catalog = CapabilityCatalog()
    catalog.register(slow_spec)
    result = anyio.run(execute, catalog, "search_files", {"query": "annek"})
    assert not result.ok and result.error_code == "timeout"


# ---- folders, ranking, not-found folders (real-use bug 2026-11-19: "find folder amma") ----


def _amma(pc: Layout) -> None:
    write(pc.docs / "Downloads" / "GrammarlyInstaller.exe")
    write(pc.docs / "Desktop" / "amma" / "birthday.jpg")
    scan(pc.index, pc.policy)


def test_search_finds_a_folder_and_says_it_is_one(pc: Layout) -> None:
    _amma(pc)
    out = _call(pc, "search_files", query="amma")
    first = out.results[0]
    assert (Path(first.path).name, first.type, first.size_bytes) == ("amma", "folder", 0)
    assert out.results[-1].type == "file" and _names(out)[-1] == "GrammarlyInstaller.exe"


def test_type_folder_returns_only_folders(pc: Layout) -> None:
    _amma(pc)
    out = _call(pc, "search_files", query="amma", type="folder")
    assert [(Path(h.path).name, h.type) for h in out.results] == [("amma", "folder")]


@pytest.mark.parametrize("query", ["folder amma", "amma folder", "the folder called amma"])
def test_the_word_folder_in_a_query_means_look_for_folders(pc: Layout, query: str) -> None:
    _amma(pc)
    out = _call(pc, "search_files", query=query)
    assert _names(out)[0] == "amma" and out.results[0].type == "folder"


def test_an_extension_never_returns_folders(pc: Layout) -> None:
    write(pc.docs / "reports.docx" / "inner.txt")  # a folder that looks like a file name
    scan(pc.index, pc.policy)
    out = _call(pc, "search_files", query="reports", extension="docx")
    assert out.results == []


def test_a_folder_gone_since_the_scan_is_not_returned(pc: Layout) -> None:
    _amma(pc)
    for child in (pc.docs / "Desktop" / "amma").iterdir():
        child.unlink()
    (pc.docs / "Desktop" / "amma").rmdir()
    out = _call(pc, "search_files", query="amma", type="folder")
    assert out.results == [] and out.match == "none"


def test_not_found_names_the_searched_folders_when_a_folder_is_outside(pc: Layout) -> None:
    write(pc.outside / "amma" / "x.txt")  # exists on the PC, but not in an approved folder
    scan(pc.index, pc.policy)
    out = _call(pc, "search_files", query="amma", type="folder")
    assert out.match == "none"
    assert f"Searched folders (and everything inside them): {pc.docs}; {pc.workspace}." in (
        out.note or ""
    )
    assert str(pc.outside) not in (out.note or "")


def test_recent_files_and_disk_usage_list_files_not_folders(pc: Layout) -> None:
    _amma(pc)
    recent = _call(pc, "list_recent_files", days=1, limit=50)
    assert all(hit.type == "file" for hit in recent.results)
    assert "amma" not in _names(recent)


def test_only_letters_inside_longer_words_is_flagged_as_weak(pc: Layout) -> None:
    write(pc.docs / "Downloads" / "GrammarlyInstaller.exe")
    scan(pc.index, pc.policy)
    out = _call(pc, "search_files", query="amma")
    assert _names(out) == ["GrammarlyInstaller.exe"]
    assert out.note == file_tools.WEAK_MATCH_NOTE


def test_a_real_match_is_not_flagged_as_weak(pc: Layout) -> None:
    _amma(pc)
    assert _call(pc, "search_files", query="amma").note is None
    assert _call(pc, "search_files", query="annek").note is None


# ---- answers built by code, without a second model call (2026-11-19) ----


def test_search_answer_lists_folders_and_files_best_first(pc: Layout) -> None:
    _amma(pc)
    out = _call(pc, "search_files", query="amma")
    text = file_tools.answer_search_files(out)
    assert text is not None
    lines = text.splitlines()
    assert lines[0] == "Found in the approved folders, best match first:"
    assert (
        lines[1]
        == f"1. Folder: {pc.docs / 'Desktop' / 'amma'} (modified {out.results[0].modified})"
    )
    assert lines[-1].startswith("3. File: ") and "GrammarlyInstaller.exe" in lines[-1]


@pytest.mark.parametrize("query", ["quarterly-forecast", "amma", "zebra invoice proposal"])
def test_search_answer_leaves_not_found_and_weak_matches_to_the_model(
    pc: Layout, query: str
) -> None:
    # "amma": a weak match only; "zebra invoice proposal": only some words match
    write(pc.docs / "Downloads" / "GrammarlyInstaller.exe")
    scan(pc.index, pc.policy)
    assert file_tools.answer_search_files(_call(pc, "search_files", query=query)) is None


def test_search_answer_says_how_many_more(pc: Layout) -> None:
    for i in range(8):
        write(pc.docs / "many" / f"report {i}.txt")
    scan(pc.index, pc.policy)
    text = file_tools.answer_search_files(_call(pc, "search_files", query="report", limit=8))
    assert text is not None and text.count("\n") == 6 and text.endswith("…and 3 more.")


def test_recent_and_recycle_bin_answers(pc: Layout) -> None:
    recent = file_tools.answer_list_recent_files(_call(pc, "list_recent_files", days=1))
    assert recent is not None and recent.startswith("Changed in the last 1 day(s), newest first:")
    gone = pc.docs / "projects" / "old-thesis.docx"
    bin_ = FakeBin([_deleted(pc, gone)])
    found = file_tools.answer_check_recycle_bin(
        _call(pc, "check_recycle_bin", bin_, query="thesis")
    )
    assert found is not None and str(gone) in found and found.splitlines()[-1].startswith("I can")
    empty = _call(pc, "check_recycle_bin", FakeBin(), query="thesis")
    assert file_tools.answer_check_recycle_bin(empty) is None


def test_disk_usage_answer(pc: Layout) -> None:
    text = file_tools.answer_get_disk_usage(_call(pc, "get_disk_usage"))
    assert text is not None and text.startswith("Drives:") and str(pc.docs) in text


def test_only_lookups_and_unapproved_local_writes_answer_from_code() -> None:
    from agents.runtime.policy import _answer
    from integrations.mcp.tool_schemas import ToolResult

    catalog = default_catalog()
    for spec in catalog.all_tools():
        if spec.answer is not None:  # step 15: also planner entries and new workspace documents
            assert spec.manifest.side_effects != "external_action"
            assert not spec.manifest.approval_required
    search = catalog.get("search_files")
    assert search is not None and search.answer is not None
    assert _answer(search, ToolResult.failure("tool_error", "boom")) is None
    write_tool = catalog.get("delete_file")
    assert write_tool is not None and _answer(write_tool, ToolResult(ok=True, data={})) is None
