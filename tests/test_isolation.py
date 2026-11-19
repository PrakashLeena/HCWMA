"""Every test runs on temp data, never on the owner's real memory, audit log, index, tool
state or credentials (tests/conftest.py)."""

from __future__ import annotations

import os
from pathlib import Path

import keyring
import pytest

from memory.config import resolve_data_dir
from memory.pc_index.index import FileIndex
from security.audit.audit_logger import get_audit_logger
from tests import conftest


def test_every_default_location_is_inside_this_tests_temp_folder(tmp_path: Path) -> None:
    for path in (
        resolve_data_dir(),
        get_audit_logger().path,
        FileIndex().db_path,
        Path(os.environ["HCWMA_GENERATED_DIR"]),
    ):
        assert tmp_path in Path(path).parents, path
    assert os.environ["HCWMA_DISABLE_KEYRING"] == "1"


def test_the_credential_store_is_in_memory() -> None:
    assert isinstance(keyring.get_keyring(), conftest.MemoryKeyring)
    keyring.set_password("hcwma", "probe", "x")
    assert keyring.get_password("hcwma", "probe") == "x"


@pytest.mark.parametrize("root", range(len(conftest.REAL_ROOTS)))
def test_touching_real_data_is_refused_and_recorded(root: int) -> None:
    # Deleting a file that does not exist: harmless even if the guard ever stopped working.
    target = Path(conftest.REAL_ROOTS[root]) / "isolation-probe-does-not-exist.txt"
    with pytest.raises(PermissionError, match="test isolation"):
        os.remove(target)
    violations = conftest._state["violations"]
    assert isinstance(violations, list) and violations
    violations.clear()  # this test touched it on purpose; do not fail it


def test_reading_the_owners_data_folder_is_refused_too() -> None:
    target = Path(conftest.REAL_ROOTS[3]) / "memory" / "memory.sqlite3"
    with pytest.raises(PermissionError, match="test isolation"):
        target.open("rb")
    with pytest.raises(PermissionError, match="test isolation"):
        os.listdir(conftest.REAL_ROOTS[3])
    violations = conftest._state["violations"]
    assert isinstance(violations, list) and len(violations) == 2
    violations.clear()
