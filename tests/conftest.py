"""Shared test setup.

Tests must never touch the owner's real memory database or backups (build step 10): every
test gets its own empty HCWMA data folder through `HCWMA_DATA_DIR`. Child processes started
by a test (the stdio MCP server) inherit it.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_hcwma_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HCWMA_DATA_DIR", str(tmp_path / "hcwma-data"))
    # Tool Builder output (step 13): tests never read or write the real integrations/generated/.
    monkeypatch.setenv("HCWMA_GENERATED_DIR", str(tmp_path / "generated"))
