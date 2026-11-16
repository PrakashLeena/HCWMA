"""Load and validate `configs/backend.yaml`. The backend binds to 127.0.0.1 only (rule 8)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / "configs" / "backend.yaml"


class BackendConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # A Literal, not a str: no config value can ever make the backend listen elsewhere.
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)
    chat_max_tokens: int = Field(default=512, ge=1, le=4096)


def load_backend_config(path: Path | None = None) -> BackendConfig:
    with (path or DEFAULT_CONFIG_PATH).open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return BackendConfig.model_validate(data)
