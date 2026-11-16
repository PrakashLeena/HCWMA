"""Load and validate `configs/model.yaml`. Changing runtime or checkpoint is a config change."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, field_validator

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "configs" / "model.yaml"

LOCAL_HOSTS = {"127.0.0.1"}


class Endpoint(BaseModel):
    host: str
    port: int

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        # Architecture rule 8: every service is reached on 127.0.0.1 only.
        if value not in LOCAL_HOSTS:
            raise ValueError(f"model endpoint must be 127.0.0.1, got {value!r}")
        return value

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class ModelConfig(BaseModel):
    checkpoint: str
    fallback_checkpoint: str | None = None
    runtime: Literal["ollama"]
    ollama_model: str
    quantization: str
    device: Literal["cpu", "gpu"]
    endpoint: Endpoint
    max_parallel_requests: int = 1
    context_length: int = 8192
    think: bool = False
    keep_alive: str = "30m"
    request_timeout_s: float = 600.0
    research_checkpoint_dir: str | None = None

    @field_validator("max_parallel_requests")
    @classmethod
    def _one_instance(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_parallel_requests must be >= 1")
        return value


def load_model_config(path: Path | None = None) -> ModelConfig:
    with (path or DEFAULT_CONFIG_PATH).open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return ModelConfig.model_validate(data)
