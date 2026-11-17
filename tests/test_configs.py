"""Step 2: config templates parse and keep the strict defaults from the master document."""

from pathlib import Path
from typing import Any

import yaml

CONFIGS = Path(__file__).resolve().parent.parent / "configs"


def _load(name: str) -> dict[str, Any]:
    with (CONFIGS / name).open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict)
    return data


def test_permissions_tiers_and_defaults() -> None:
    perms = _load("permissions.yaml")
    tiers = perms["tiers"]
    assert set(tiers) == {"T0", "T1", "T2", "T3", "T4"}
    assert tiers["T3"]["default"] == "approval_required"
    assert tiers["T4"]["default"] == "deny"


def test_permissions_start_strict() -> None:
    perms = _load("permissions.yaml")
    # Owner decision (Step 2): no folders approved yet.
    assert perms["approved_folders"] == []
    assert perms["workspace"] == "workspace"
    protected = perms["protected_paths"]
    for required in ["~/.ssh", "security/"]:
        assert required in protected
    assert perms["delete_mode"] == "recycle_bin"
    assert perms["limits"]["max_delegation_depth"] == 3
    assert perms["limits"]["max_tool_calls_per_task"] == 20


def test_model_config_is_local_only() -> None:
    model = _load("model.yaml")
    assert model["runtime"] == "ollama"
    assert model["device"] == "cpu"
    assert model["endpoint"]["host"] == "127.0.0.1"
    assert model["max_parallel_requests"] == 1


def test_no_config_binds_all_interfaces() -> None:
    for path in CONFIGS.glob("*.yaml"):
        assert "0.0.0.0" not in path.read_text(encoding="utf-8"), path.name


def test_memory_config_matches_the_owner_decisions() -> None:
    from memory.config import load_memory_config

    cfg = load_memory_config()
    assert cfg.data_dir == "default"  # outside the repository (owner decision 2026-11-17)
    assert cfg.retention_days == {
        "profile": None,
        "episodic": 365,
        "summaries": 90,
        "skill_notes": None,
    }
    assert cfg.skill_note_review_days == 180 and cfg.audit_retention_days == 180
    assert cfg.retrieval.max_entries == 5 and cfg.retrieval.max_tokens == 300
    assert cfg.backup.every_hours == 24 and cfg.backup.keep == 7


def test_memory_data_folder_is_outside_the_repository(monkeypatch) -> None:
    from memory.config import default_data_dir

    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\someone\AppData\Local")
    assert default_data_dir().name == "HCWMA"
    assert not default_data_dir().is_relative_to(Path(__file__).resolve().parents[1])
