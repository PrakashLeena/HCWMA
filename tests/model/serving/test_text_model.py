"""2026-11-21: the text-only tag reuses the weights layer and drops the vision projector."""

from __future__ import annotations

from pathlib import Path

import pytest

from model.serving.config import load_model_config
from model.serving.text_model import (
    MODEL_LAYER,
    PROJECTOR_LAYER,
    create_request,
    manifest_path,
    weights_digest,
)

MANIFEST = {
    "layers": [
        {"mediaType": PROJECTOR_LAYER, "digest": "sha256:proj"},
        {"mediaType": MODEL_LAYER, "digest": "sha256:weights"},
        {"mediaType": "application/vnd.ollama.image.license", "digest": "sha256:lic"},
    ]
}
SHOW = {
    "template": "{{ .Prompt }}",
    "renderer": "qwen3.5",
    "parser": "qwen3.5",
    "parameters": 'presence_penalty 1.5\ntemperature 1\ntop_k 20\ntop_p 0.95\nstop "<|im_end|>"',
}


def test_only_the_weights_layer_is_used() -> None:
    assert weights_digest(MANIFEST) == "sha256:weights"
    with pytest.raises(ValueError):
        weights_digest({"layers": [{"mediaType": PROJECTOR_LAYER, "digest": "x"}]})


def test_same_renderer_parser_and_parameters() -> None:
    body = create_request("qwen3.5-text:4b", "sha256:weights", SHOW)
    assert body["files"] == {"model.gguf": "sha256:weights"}
    assert "sha256:proj" not in str(body)
    assert body["renderer"] == body["parser"] == "qwen3.5"
    assert body["template"] == "{{ .Prompt }}"
    assert body["parameters"] == {
        "presence_penalty": 1.5,
        "temperature": 1,
        "top_k": 20,
        "top_p": 0.95,
        "stop": "<|im_end|>",
    }


def test_manifest_path() -> None:
    root = Path("E:/models")
    assert manifest_path(root, "qwen3.5:4b") == (
        root / "manifests" / "registry.ollama.ai" / "library" / "qwen3.5" / "4b"
    )
    assert manifest_path(root, "me/thing").parts[-2:] == ("thing", "latest")


def test_model_override_for_ab_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HCWMA_OLLAMA_MODEL", "qwen3.5:4b")
    assert load_model_config().ollama_model == "qwen3.5:4b"
    monkeypatch.delenv("HCWMA_OLLAMA_MODEL")
    assert load_model_config().ollama_model == "qwen3.5-text:4b"
