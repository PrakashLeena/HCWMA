"""Step 5: the backend config only ever binds to 127.0.0.1 (architecture rule 8)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.config import BackendConfig, load_backend_config


def test_default_config_is_loopback() -> None:
    config = load_backend_config()
    assert config.host == "127.0.0.1"
    assert 1024 <= config.port <= 65535


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "localhost", ""])
def test_any_other_host_is_refused(host: str) -> None:
    with pytest.raises(ValidationError):
        BackendConfig(host=host, port=8765)


def test_config_file_with_open_host_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "backend.yaml"
    path.write_text("host: 0.0.0.0\nport: 8765\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_backend_config(path)


@pytest.mark.parametrize("port", [0, 80, 70000])
def test_bad_port_is_refused(port: int) -> None:
    with pytest.raises(ValidationError):
        BackendConfig(host="127.0.0.1", port=port)
