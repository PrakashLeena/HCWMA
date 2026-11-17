"""Load and validate `configs/memory.yaml`, and work out where the memory data lives."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from memory.scopes import STORED_SCOPES

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / "configs" / "memory.yaml"
DATA_DIR_ENV = "HCWMA_DATA_DIR"
DB_NAME = "memory.sqlite3"


class Retrieval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Hard ceilings from the memory policy (section 5); config can only go lower.
    max_entries: int = Field(default=5, ge=1, le=5)
    max_tokens: int = Field(default=300, ge=50, le=300)


class Backup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    every_hours: int = Field(default=24, ge=1, le=24 * 30)
    keep: int = Field(default=7, ge=1, le=60)


class MemoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    data_dir: str = "default"
    retention_days: dict[str, int | None] = Field(
        default_factory=lambda: {
            "profile": None,
            "episodic": 365,
            "summaries": 90,
            "skill_notes": None,
        }
    )
    skill_note_review_days: int = Field(default=180, ge=1)
    audit_retention_days: int = Field(default=180, ge=1)
    retrieval: Retrieval = Field(default_factory=Retrieval)
    max_entry_chars: int = Field(default=500, ge=50, le=2000)
    backup: Backup = Field(default_factory=Backup)
    summarize_every_turns: int = Field(default=4, ge=0, le=50)

    @field_validator("retention_days")
    @classmethod
    def _all_scopes(cls, value: dict[str, int | None]) -> dict[str, int | None]:
        if set(value) != set(STORED_SCOPES):
            raise ValueError(f"retention_days must name exactly {STORED_SCOPES}")
        if any(days is not None and days < 1 for days in value.values()):
            raise ValueError("retention_days must be positive or null")
        return value


def load_memory_config(path: Path | None = None) -> MemoryConfig:
    with (path or DEFAULT_CONFIG_PATH).open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return MemoryConfig.model_validate(data)


def default_data_dir() -> Path:
    """The per-user application data folder for HCWMA, outside the repository."""
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "HCWMA"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "hcwma"


def resolve_data_dir(config: MemoryConfig | None = None) -> Path:
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return Path(override)
    cfg = config or load_memory_config()
    if cfg.data_dir == "default":
        return default_data_dir()
    path = Path(cfg.data_dir).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


def memory_db_path(data_dir: Path) -> Path:
    return data_dir / "memory" / DB_NAME


def backups_dir(data_dir: Path) -> Path:
    return data_dir / "backups"
