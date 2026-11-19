"""Step 18: full backups (memory, planner, tool state, settings, configs), keep 7, restore on a
clean folder, and a damaged or forged backup is refused without changing anything."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backend.reliability import backup as backup_module
from backend.reliability.__main__ import main as cli
from backend.reliability.backup import MANIFEST, BackupManager, BackupRefused
from backend.reliability.settings import SettingsStore
from integrations.adapters.local_data.planner_store import PlannerStore, planner_db_path
from memory.service import MemoryService
from memory.storage.store import MemoryStore

NOW = datetime(2026, 11, 19, 8, 0)


class Clock(datetime):
    now_value = datetime(2026, 11, 19, 9, 0, 0)

    @classmethod
    def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
        return cls.now_value


@pytest.fixture
def trashed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """A stand-in Recycle Bin: records what was sent and moves it out of the way."""
    moved: list[str] = []
    bin_dir = tmp_path / "recycle-bin"
    bin_dir.mkdir()

    def fake_send2trash(path: str) -> None:
        moved.append(path)
        Path(path).rename(bin_dir / Path(path).name)

    monkeypatch.setattr(backup_module, "send2trash", fake_send2trash)
    return moved


def _fill(data: Path, configs: Path) -> tuple[MemoryService, PlannerStore]:
    memory = MemoryService.from_config()
    fact = "My sister is called Anu"
    assert (
        memory.write(fact, scope="profile", source="owner_said", owner_words=fact).status
        == "stored"
    )
    planner = PlannerStore(planner_db_path(data))
    planner.add("reminder", "pay the rent", datetime(2026, 11, 20, 9, 0), NOW)
    tools = data / "generated_tools"
    (tools / "snapshots").mkdir(parents=True)
    (tools / "state.json").write_text('{"word_count": {"approved": {"hash": "abc"}}}', "utf-8")
    (tools / "snapshots" / "word_count-abc.py").write_text("def run(): ...\n", "utf-8")
    (tools / "run").mkdir()
    (tools / "run" / "temp.txt").write_text("scratch", "utf-8")  # not state: not backed up
    SettingsStore(data).update(heavy_only_when_plugged_in=True)
    configs.mkdir(parents=True)
    (configs / "permissions.yaml").write_text("approved_folders: []\n", "utf-8")
    (configs / "model.yaml").write_text("runtime: ollama\n", "utf-8")
    return memory, planner


@pytest.fixture
def data(tmp_path: Path) -> Path:
    return tmp_path / "hcwma-data"  # the HCWMA_DATA_DIR set by tests/conftest.py


def test_backup_holds_every_part_and_restores_on_a_clean_folder(data: Path, tmp_path: Path) -> None:
    configs = tmp_path / "configs"
    _fill(data, configs)
    folder = BackupManager(data, configs).backup(label="test")
    manifest = json.loads((folder / MANIFEST).read_text("utf-8"))
    parts = {item["path"] for item in manifest["items"]}
    assert parts == {
        "memory/memory.sqlite3",
        "planner/planner.sqlite3",
        "generated_tools/state.json",
        "generated_tools/snapshots/word_count-abc.py",
        "settings.json",
        "configs/permissions.yaml",
        "configs/model.yaml",
    }
    assert folder.parent == data / "backups" and folder.name.startswith("full-")

    clean, clean_configs = tmp_path / "brand-new-pc", tmp_path / "brand-new-configs"
    assert not clean.exists()
    report = BackupManager(clean, clean_configs).restore(folder)
    assert report.safety_backup is None  # nothing there to protect
    assert report.memory_entries == 1 and report.planner_items == 1
    assert [e.text for e in MemoryStore(clean / "memory" / "memory.sqlite3").list()] == [
        "My sister is called Anu"
    ]
    items = PlannerStore(planner_db_path(clean)).items()
    assert [(i.title, i.due) for i in items] == [("pay the rent", "2026-11-20T09:00")]
    assert json.loads((clean / "generated_tools" / "state.json").read_text("utf-8"))["word_count"]
    assert not (clean / "generated_tools" / "run").exists()
    assert SettingsStore(clean).load().heavy_only_when_plugged_in is True
    assert (clean_configs / "permissions.yaml").read_text("utf-8") == "approved_folders: []\n"


def test_restore_over_existing_data_backs_it_up_first(data: Path, tmp_path: Path) -> None:
    configs = tmp_path / "configs"
    memory, planner = _fill(data, configs)
    manager = BackupManager(data, configs)
    good = manager.backup()
    planner.add("todo", "something added after the backup", None, NOW)
    report = manager.restore(good)
    assert report.safety_backup is not None and Path(report.safety_backup).is_dir()
    assert [i.title for i in planner.items()] == ["pay the rent"]
    # and the state from before the restore can itself be restored
    manager.restore(Path(report.safety_backup), safety_backup=False)
    assert len(planner.items()) == 2


def test_keeps_the_newest_seven_and_older_go_to_the_recycle_bin(
    data: Path, tmp_path: Path, trashed: list[str]
) -> None:
    _fill(data, tmp_path / "configs")
    manager = BackupManager(data, tmp_path / "configs", clock=Clock)
    made = []
    for day in range(9):
        Clock.now_value = datetime(2026, 11, 1, 3, 0) + timedelta(days=day)
        made.append(manager.backup())
    assert trashed == [str(made[0]), str(made[1])]  # moved, never erased
    assert manager.keep == 7


def test_a_backup_is_due_once_a_day(data: Path, tmp_path: Path) -> None:
    manager = BackupManager(data, tmp_path / "configs", clock=Clock)
    assert manager.due()  # none yet
    Clock.now_value = datetime.now()
    manager.backup()
    assert not manager.due()
    Clock.now_value = datetime.now() + timedelta(hours=25)
    assert manager.due()


def test_a_damaged_backup_is_refused_and_nothing_changes(data: Path, tmp_path: Path) -> None:
    configs = tmp_path / "configs"
    _, planner = _fill(data, configs)
    manager = BackupManager(data, configs)
    folder = manager.backup()
    (folder / "configs" / "permissions.yaml").write_text("approved_folders: ['C:/']\n", "utf-8")
    with pytest.raises(BackupRefused, match="checksum"):
        manager.restore(folder)
    assert (configs / "permissions.yaml").read_text("utf-8") == "approved_folders: []\n"
    assert len(manager.backups()) == 1  # not even the safety backup was made

    (folder / "planner" / "planner.sqlite3").write_bytes(b"not a database at all" * 50)
    with pytest.raises(BackupRefused):
        manager.check(folder)
    assert len(planner.items()) == 1


@pytest.mark.parametrize(
    "path",
    ["../outside.txt", "configs/../../evil.yaml", "C:/Windows/x.yaml", "security/keys.yaml",
     "generated_tools/run/x.py", "configs/sub/deep.yaml", "/etc/passwd"],
)  # fmt: skip
def test_a_forged_manifest_cannot_write_anywhere_else(
    path: str, data: Path, tmp_path: Path
) -> None:
    configs = tmp_path / "configs"
    _fill(data, configs)
    manager = BackupManager(data, configs)
    folder = manager.backup()
    manifest = json.loads((folder / MANIFEST).read_text("utf-8"))
    manifest["items"].append({"path": path, "kind": "file", "sha256": "0" * 64, "size": 1})
    (folder / MANIFEST).write_text(json.dumps(manifest), "utf-8")
    with pytest.raises(BackupRefused, match="never writes"):
        manager.restore(folder)


def test_forgetting_a_memory_also_removes_it_from_full_backups(data: Path, tmp_path: Path) -> None:
    memory, _ = _fill(data, tmp_path / "configs")
    folder = BackupManager(data, tmp_path / "configs").backup()
    [entry] = memory.everything()
    memory.forget([entry.id])
    assert MemoryStore(folder / "memory" / "memory.sqlite3").list() == []
    BackupManager(data, tmp_path / "configs").check(folder)  # still a valid backup


def test_the_restore_command_on_a_clean_folder(
    data: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configs = tmp_path / "configs"
    _fill(data, configs)
    folder = BackupManager(data, configs).backup()
    clean = tmp_path / "clean"
    code = cli(
        ["restore", str(folder), "--yes", "--data-dir", str(clean), "--configs-dir",
         str(tmp_path / "clean-configs")]
    )  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0, out
    assert "memory: 1 entries" in out and "planner: 1 items" in out


def test_restore_refuses_while_hcwma_is_running(data: Path, tmp_path: Path) -> None:
    from backend.reliability.instance_lock import InstanceLock

    configs = tmp_path / "configs"
    _fill(data, configs)
    folder = BackupManager(data, configs).backup()
    running = InstanceLock(data)
    assert running.acquire("desktop app")
    try:
        assert cli(["restore", str(folder), "--yes"]) == 1
    finally:
        running.release()
