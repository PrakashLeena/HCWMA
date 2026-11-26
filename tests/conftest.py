"""Shared test setup.

Tests must never touch the owner's real memory database or backups (build step 10) or the
real audit log: every test gets its own empty HCWMA data folder through `HCWMA_DATA_DIR` and
its own audit file through `HCWMA_AUDIT_PATH`. Child processes started
by a test (the stdio MCP server) inherit it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_hcwma_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HCWMA_DATA_DIR", str(tmp_path / "hcwma-data"))
    # Tool Builder output (step 13): tests never read or write the real integrations/generated/.
    monkeypatch.setenv("HCWMA_GENERATED_DIR", str(tmp_path / "generated"))
    # Gmail (step 15): tests never read or change the owner's real credential store.
    monkeypatch.setenv("HCWMA_DISABLE_KEYRING", "1")
    # Audit log: tests never append to the owner's real security/audit/logs/audit.jsonl
    # (CLI code such as `python -m memory` and `python -m backend.reliability` uses the
    # shared logger, which used to always write there).
    monkeypatch.setenv("HCWMA_AUDIT_PATH", str(tmp_path / "audit" / "audit.jsonl"))
    # App launching (2026-11-20): the real launcher never opens WhatsApp, Calculator or Notepad
    # during tests (tests use a fake starter).
    monkeypatch.setenv("HCWMA_DISABLE_APP_LAUNCH", "1")
    # A/B model runs (2026-11-21): tests always see the model named in configs/model.yaml.
    monkeypatch.delenv("HCWMA_OLLAMA_MODEL", raising=False)
    # Adapters (2026-11-21): a new install starts with every adapter off. Tests of the
    # WhatsApp / Gmail tools expect them on, as on an install that already used them; tests
    # of the on/off rule write their own state.
    data = tmp_path / "hcwma-data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "adapters.json").write_text(
        json.dumps(
            {
                "adapters": {
                    a: {"on": True, "since": "test", "source": "test"}
                    for a in ("whatsapp", "gmail")
                }
            }
        ),
        encoding="utf-8",
    )
