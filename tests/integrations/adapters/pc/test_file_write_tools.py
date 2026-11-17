"""Step 9: `restore_file`, `move_file`, `delete_file` (add-tool skill test groups).

Everything runs on a throw-away "PC" in a temp folder with a fake Recycle Bin on disk.
`delete_file` is pointed at a recorder instead of the real Recycle Bin, so no test touches
the owner's files or bin. The approval prompt itself is tested in
`tests/agents/runtime/test_approval_flow.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from agents.runtime.policy import PolicyLayer
from integrations.adapters.pc import file_write_tools
from integrations.adapters.pc.file_tools import FileToolDeps, file_tool_specs
from integrations.adapters.pc.file_write_tools import (
    DeleteFileInput,
    MoveFileInput,
    RestoreFileInput,
    ToolRefused,
    file_write_tool_specs,
)
from integrations.adapters.pc.recycle_bin import DestinationCheck, RecycledItem
from integrations.adapters.pc.windows import BIN_DIR_NAME, WindowsRecycleBin
from integrations.mcp.client import McpToolClient
from integrations.mcp.server import build_server, execute
from integrations.mcp.tool_schemas import PermissionTier, ToolSpec
from integrations.registry.capability_catalog import CapabilityCatalog, default_catalog
from memory.pc_index.scanner import scan
from security.audit.audit_logger import AuditLogger
from security.permissions.path_check import PathDenied
from security.permissions.permission_manager import (
    AgentGrant,
    PermissionManager,
    load_permissions_config,
)
from tests.integrations.adapters.pc.test_windows_recycle_bin import record_v2
from tests.pc_layout import Layout, make_layout, write

WRITE_TOOLS = {"restore_file", "move_file", "delete_file"}


class FakeDriveBin(WindowsRecycleBin):
    """The real Windows bin code, pointed at a fake drive in the temp folder."""

    def __init__(self, drive: Path) -> None:
        self.drive = drive

    def list_items(self, drives: Sequence[Path]) -> list[RecycledItem]:
        return super().list_items([self.drive])

    def find(self, item_id: str, drives: Sequence[Path]) -> RecycledItem | None:
        return super().find(item_id, [self.drive])

    def restore(self, item_id: str, drives: Sequence[Path], allow: DestinationCheck) -> Path:
        return super().restore(item_id, [self.drive], allow)


class WritePc:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.layout: Layout = make_layout(tmp_path)
        scan(self.layout.index, self.layout.policy)
        self.drive = self.layout.tmp / "drive"
        self.bin_dir = self.drive / BIN_DIR_NAME / "S-1-5-21-1-2-3-1001"
        self.bin_dir.mkdir(parents=True)
        self.bin = FakeDriveBin(self.drive)
        self.deps = FileToolDeps(
            index=self.layout.index, policy=self.layout.policy, recycle_bin=self.bin
        )
        self.trashed: list[Path] = []
        monkeypatch.setattr(file_write_tools, "move_to_recycle_bin", self._trash)

    def _trash(self, path: Path) -> None:
        """Stand-in for send2trash: the file leaves its folder, and we remember it."""
        self.trashed.append(path)
        path.rename(self.layout.tmp / f"trashed-{len(self.trashed)}-{path.name}")

    def recycle(self, original: Path, code: str = "AB12CD", text: str = "old data") -> str:
        """Put a deleted file into the fake bin. Returns its item_id."""
        name = code + original.suffix
        (self.bin_dir / f"$I{name}").write_bytes(record_v2(str(original), size=len(text)))
        (self.bin_dir / f"$R{name}").write_text(text, encoding="utf-8")
        return f"{self.drive.drive or self.drive.name}|$R{name}"

    def specs(self) -> dict[str, ToolSpec]:
        return {s.name: s for s in file_write_tool_specs(self.deps, status="approved")}

    def catalog(self) -> CapabilityCatalog:
        catalog = CapabilityCatalog()
        for spec in [*file_tool_specs(self.deps, status="approved"), *self.specs().values()]:
            catalog.register(spec)
        return catalog


@pytest.fixture
def pc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WritePc:
    return WritePc(tmp_path, monkeypatch)


def _indexed(pc: WritePc, word: str) -> list[str]:
    return [e.path for e in pc.layout.index.search([word], limit=20)]


# ---------- manifests ----------


def test_manifests_are_high_tier_and_always_need_approval(pc: WritePc) -> None:
    specs = pc.specs()
    assert set(specs) == WRITE_TOOLS
    for spec in specs.values():
        assert spec.manifest.approval_required and spec.manifest.side_effects == "writes"
        assert spec.describe is not None
    assert specs["restore_file"].manifest.permission_tier == PermissionTier.T3
    assert specs["move_file"].manifest.permission_tier == PermissionTier.T3
    assert specs["delete_file"].manifest.permission_tier == PermissionTier.T4


def test_new_tools_are_quarantined_until_the_owner_approves() -> None:
    assert all(s.manifest.status == "quarantined" for s in file_write_tool_specs(None))  # type: ignore[arg-type]
    shipped = {s.name: s.manifest.status for s in default_catalog().all_tools()}
    assert WRITE_TOOLS <= set(shipped)


# ---------- move_file ----------


def test_move_file_moves_and_renames_and_updates_the_index(pc: WritePc) -> None:
    source = pc.layout.docs / "projects" / "budget 2026.xlsx"
    out = pc.specs()["move_file"].handler(
        MoveFileInput(source=str(source), destination=str(pc.layout.docs / "notes"))
    )
    moved = pc.layout.docs / "notes" / "budget 2026.xlsx"
    assert out.moved and Path(out.destination) == moved
    assert moved.read_text(encoding="utf-8") == "budget!!" and not source.exists()
    assert _indexed(pc, "budget") == [str(moved)]

    renamed = pc.layout.docs / "notes" / "budget-final.xlsx"
    pc.specs()["move_file"].handler(MoveFileInput(source=str(moved), destination=str(renamed)))
    assert renamed.is_file() and not moved.exists()


def test_move_file_never_overwrites_and_needs_an_existing_folder(pc: WritePc) -> None:
    move = pc.specs()["move_file"].handler
    source = pc.layout.docs / "projects" / "budget 2026.xlsx"
    taken = pc.layout.docs / "notes" / "meeting notes.txt"
    with pytest.raises(ToolRefused, match="already exists"):
        move(MoveFileInput(source=str(source), destination=str(taken)))
    with pytest.raises(ToolRefused, match="does not exist"):
        move(MoveFileInput(source=str(source), destination=str(pc.layout.docs / "nope" / "b.x")))
    with pytest.raises(ToolRefused, match="folder"):
        move(MoveFileInput(source=str(pc.layout.docs / "projects"), destination="x"))
    assert source.is_file() and taken.read_text(encoding="utf-8") == "notes"


@pytest.mark.parametrize("where", ["outside", "secrets", "ssh"])
def test_move_file_refuses_protected_and_unapproved_paths(pc: WritePc, where: str) -> None:
    lay = pc.layout
    bad = {
        "outside": lay.outside / "annek-private.txt",
        "secrets": lay.secrets / "annek-passwords.txt",
        "ssh": lay.home / ".ssh" / "id_ed25519",
    }[where]
    good = lay.docs / "notes" / "meeting notes.txt"
    move = pc.specs()["move_file"].handler
    with pytest.raises(PathDenied):  # cannot take a file out of a place it may not touch
        move(MoveFileInput(source=str(bad), destination=str(lay.docs / "notes" / "x.txt")))
    with pytest.raises(PathDenied):  # and cannot put a file there either
        move(MoveFileInput(source=str(good), destination=str(bad.parent / "dropped.txt")))
    assert bad.is_file() and good.is_file()


def test_move_file_refuses_a_symlink_that_leaves_the_approved_folder(pc: WritePc) -> None:
    link = pc.layout.docs / "notes" / "shortcut.txt"
    try:
        link.symlink_to(pc.layout.outside / "annek-private.txt")
    except OSError:
        pytest.skip("this Windows account may not create symlinks (needs Developer Mode)")
    with pytest.raises(PathDenied):
        pc.specs()["move_file"].handler(
            MoveFileInput(source=str(link), destination=str(pc.layout.docs / "notes" / "y.txt"))
        )


# ---------- delete_file ----------


def test_delete_file_only_ever_uses_the_recycle_bin(pc: WritePc) -> None:
    target = pc.layout.docs / "notes" / "meeting notes.txt"
    out = pc.specs()["delete_file"].handler(DeleteFileInput(path=str(target)))
    assert out.moved_to_recycle_bin and pc.trashed == [target]
    assert not target.exists() and _indexed(pc, "meeting") == []
    source = Path(file_write_tools.__file__).read_text(encoding="utf-8")
    for forbidden in ("os.remove", "unlink", "rmtree", "rmdir"):
        assert forbidden not in source


def test_real_delete_goes_through_send2trash(monkeypatch: pytest.MonkeyPatch) -> None:
    import send2trash

    from integrations.adapters.pc.recycle_bin import move_to_recycle_bin

    sent: list[str] = []
    monkeypatch.setattr(send2trash, "send2trash", sent.append)
    move_to_recycle_bin(Path("some") / "file.txt")
    assert sent == [str(Path("some") / "file.txt")]


def test_delete_file_refuses_folders_missing_files_and_protected_paths(pc: WritePc) -> None:
    delete = pc.specs()["delete_file"].handler
    lay = pc.layout
    with pytest.raises(ToolRefused, match="folder"):
        delete(DeleteFileInput(path=str(lay.docs / "projects")))
    with pytest.raises(ToolRefused, match="no file"):
        delete(DeleteFileInput(path=str(lay.docs / "ghost.txt")))
    for bad in (lay.secrets / "annek-passwords.txt", lay.outside / "annek-private.txt", lay.docs):
        with pytest.raises(PathDenied):
            delete(DeleteFileInput(path=str(bad)))
    assert pc.trashed == []


# ---------- restore_file ----------


def test_restore_file_puts_the_file_back_and_clears_the_bin_record(pc: WritePc) -> None:
    original = pc.layout.docs / "projects" / "old-thesis.docx"
    item_id = pc.recycle(original, text="thesis text")
    assert [i.item_id for i in pc.bin.list_items([])] == [item_id]

    out = pc.specs()["restore_file"].handler(RestoreFileInput(item_id=item_id))
    assert out.restored and Path(out.path) == original
    assert original.read_text(encoding="utf-8") == "thesis text"
    assert pc.bin.list_items([]) == [] and list(pc.bin_dir.iterdir()) == []
    assert _indexed(pc, "thesis") == [str(original)]
    # an approval cannot be replayed into a second restore: the item is gone
    with pytest.raises(ToolRefused, match="not in the Recycle Bin"):
        pc.specs()["restore_file"].handler(RestoreFileInput(item_id=item_id))


def test_restore_file_never_overwrites(pc: WritePc) -> None:
    original = pc.layout.docs / "notes" / "meeting notes.txt"  # exists already
    item_id = pc.recycle(original, text="older version")
    with pytest.raises(ToolRefused, match="already exists"):
        pc.specs()["restore_file"].handler(RestoreFileInput(item_id=item_id))
    assert original.read_text(encoding="utf-8") == "notes"
    assert len(pc.bin.list_items([])) == 1  # still in the bin


@pytest.mark.parametrize("where", ["outside", "secrets"])
def test_restore_file_refuses_destinations_outside_policy(pc: WritePc, where: str) -> None:
    folder = pc.layout.outside if where == "outside" else pc.layout.secrets
    item_id = pc.recycle(folder / "old-keys.txt")
    with pytest.raises(PathDenied):
        pc.specs()["restore_file"].handler(RestoreFileInput(item_id=item_id))
    assert not (folder / "old-keys.txt").exists() and len(list(pc.bin_dir.iterdir())) == 2


@pytest.mark.parametrize(
    "item_id",
    ["nonsense", "C:|$R..\\..\\evil.txt", "C:|$I123.txt", "C:|$RAB/CD.txt", "C:|notes.txt"],
)
def test_restore_file_rejects_made_up_item_ids(pc: WritePc, item_id: str) -> None:
    write(pc.bin_dir / "notes.txt", "not a bin item")
    with pytest.raises(ToolRefused):
        pc.specs()["restore_file"].handler(RestoreFileInput(item_id=item_id))


# ---------- what the owner is shown ----------


def test_descriptions_say_exactly_what_will_happen(pc: WritePc) -> None:
    specs, lay = pc.specs(), pc.layout
    original = lay.docs / "projects" / "old-thesis.docx"
    item_id = pc.recycle(original)
    restore = specs["restore_file"].describe(RestoreFileInput(item_id=item_id))  # type: ignore[misc]
    assert str(original) in restore and "Recycle Bin" in restore
    missing = specs["restore_file"].describe(RestoreFileInput(item_id="C:|$RZZZZ.txt"))  # type: ignore[misc]
    assert "not in the Recycle Bin" in missing

    source = lay.docs / "projects" / "budget 2026.xlsx"
    move = specs["move_file"].describe(  # type: ignore[misc]
        MoveFileInput(source=str(source), destination=str(lay.docs / "notes"))
    )
    assert str(source) in move and str(lay.docs / "notes" / source.name) in move

    delete = specs["delete_file"].describe(DeleteFileInput(path=str(source)))  # type: ignore[misc]
    assert str(source) in delete and "Recycle Bin" in delete
    refused = specs["delete_file"].describe(  # type: ignore[misc]
        DeleteFileInput(path=str(lay.secrets / "annek-passwords.txt"))
    )
    assert "will refuse" in refused
    assert source.is_file()  # describing changes nothing


# ---------- through the gateway ----------


def _call(
    pc: WritePc, tmp_path: Path, agent: AgentGrant, tool: str, args: dict[str, Any]
) -> tuple[Any, AuditLogger]:
    audit = AuditLogger(tmp_path / "audit.jsonl")
    catalog = pc.catalog()

    async def go() -> Any:
        async with McpToolClient(Client(build_server(catalog))) as client:
            policy = PolicyLayer(
                client=client,
                catalog=catalog,
                permissions=PermissionManager(load_permissions_config()),
                audit=audit,
            )
            listed = {t.name for t in await policy.list_tools_for(agent)}
            return listed, await policy.call_tool("task-1", agent, tool, args)

    return anyio.run(go), audit


FILE_AGENT = AgentGrant(
    agent_id="file_system", tools=frozenset(WRITE_TOOLS), max_tier=PermissionTier.T4
)


@pytest.mark.parametrize("tool", sorted(WRITE_TOOLS))
def test_every_write_tool_is_blocked_without_an_approval_and_audited(
    pc: WritePc, tmp_path: Path, tool: str
) -> None:
    target = pc.layout.docs / "notes" / "meeting notes.txt"
    args = {
        "restore_file": {"item_id": pc.recycle(pc.layout.docs / "gone.txt")},
        "move_file": {"source": str(target), "destination": str(pc.layout.docs / "moved.txt")},
        "delete_file": {"path": str(target)},
    }[tool]
    (listed, result), audit = _call(pc, tmp_path, FILE_AGENT, tool, args)
    assert tool in listed  # discovered through MCP: the agent may propose it
    assert not result.ok and result.error_code == "needs_approval"
    assert target.is_file() and pc.trashed == [] and not (pc.layout.docs / "gone.txt").exists()
    record = audit.read_all()[-1]
    assert (record.event, record.agent, record.tool) == ("tool_call", "file_system", tool)
    assert record.outcome == "needs_approval"


def test_agent_without_the_tool_on_its_allowlist_is_refused(pc: WritePc, tmp_path: Path) -> None:
    other = AgentGrant(agent_id="echo", tools=frozenset({"search_files"}), max_tier="T4")
    target = pc.layout.docs / "notes" / "meeting notes.txt"
    (listed, result), audit = _call(pc, tmp_path, other, "delete_file", {"path": str(target)})
    assert "delete_file" not in listed
    assert result.error_code == "denied" and "allowlist" in (result.error or "")
    assert target.is_file() and audit.read_all()[-1].outcome == "denied"


def test_invalid_arguments_are_rejected_before_any_prompt(pc: WritePc, tmp_path: Path) -> None:
    (_, result), _ = _call(pc, tmp_path, FILE_AGENT, "delete_file", {"path": "", "force": True})
    assert result.error_code == "invalid_arguments"


def test_a_slow_write_tool_times_out_cleanly() -> None:
    import time

    spec = file_write_tool_specs(None, status="approved")[2]  # type: ignore[arg-type]
    slow = ToolSpec(
        manifest=spec.manifest.model_copy(update={"timeout_s": 0.05}),
        input_model=spec.input_model,
        output_model=spec.output_model,
        handler=lambda _args: time.sleep(0.5),  # type: ignore[arg-type, return-value]
    )
    catalog = CapabilityCatalog()
    catalog.register(slow)
    result = anyio.run(execute, catalog, "delete_file", {"path": "x.txt"})
    assert result.error_code == "timeout"
