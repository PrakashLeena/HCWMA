"""Step 10: `python -m memory` (show, export, backup, restore, forget, cleanup)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from memory.__main__ import main
from memory.service import MemoryService

FACT = "My sister's birthday is on 12 March"


def test_owner_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    service = MemoryService.from_config()
    assert Path(os.environ["HCWMA_DATA_DIR"]) in service.store.path.parents  # never the real one
    entry = service.write(FACT, scope="profile", source="owner_said", owner_words=FACT).entry

    assert main(["show"]) == 0 and FACT in capsys.readouterr().out
    assert main(["export", str(tmp_path / "out")]) == 0
    assert len(list((tmp_path / "out").iterdir())) == 2
    assert main(["backup"]) == 0
    (backup,) = service.backups()

    # forget removes the entry here and in the backup
    assert main(["forget", entry.id]) == 0
    assert service.everything() == [] and FACT.encode() not in backup.read_bytes()

    # restore from a copy taken elsewhere gives the entry back
    service.write(FACT, scope="profile", source="owner_said", owner_words=FACT)
    keep = tmp_path / "keep.sqlite3"
    service.store.backup_to(keep)
    service.forget_everything()
    assert main(["restore", str(keep), "--yes"]) == 0
    assert [e.text for e in MemoryService.from_config().everything()] == [FACT]

    assert main(["cleanup"]) == 0
    assert main(["restore", str(tmp_path / "nope.sqlite3"), "--yes"]) == 1
