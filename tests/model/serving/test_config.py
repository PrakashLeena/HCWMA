"""Step 3: model config is loopback-only and selects the runtime."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from model.serving.config import DEFAULT_CONFIG_PATH, load_model_config


def _write(tmp_path: Path, **overrides: object) -> Path:
    data = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text("utf-8"))
    for key, value in overrides.items():
        data[key] = value
    path = tmp_path / "model.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_repo_config_is_valid() -> None:
    config = load_model_config()
    assert config.runtime == "ollama"
    assert config.ollama_model == "qwen3.5-text:4b"
    assert config.endpoint.base_url == "http://127.0.0.1:11434"
    assert config.max_parallel_requests == 1
    assert config.device == "cpu"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com", "localhost"])
def test_non_loopback_host_rejected(tmp_path: Path, host: str) -> None:
    path = _write(tmp_path, endpoint={"host": host, "port": 11434})
    with pytest.raises(ValidationError):
        load_model_config(path)


def test_unknown_runtime_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        load_model_config(_write(tmp_path, runtime="cloud-api"))


def test_zero_parallel_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        load_model_config(_write(tmp_path, max_parallel_requests=0))
