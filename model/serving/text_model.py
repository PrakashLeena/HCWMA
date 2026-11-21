"""Make a text-only Ollama tag from an installed vision tag (no download, no conversion).

`qwen3.5:4b` stores its weights and its vision projector as separate layers. The text-only
tag uses the SAME weights layer (same file, same Q4_K_M quantization, nothing copied) with the
same renderer, parser (tool-call format) and parameters, and leaves the projector out, so
llama-server never loads it (~0.9 GB less RAM; decided 2026-11-21, docs/DECISIONS.md).

    python -m model.serving.text_model [--source qwen3.5:4b] [--name qwen3.5-text:4b]

Reads the source tag's manifest from `ollama_models_dir` and talks to Ollama on 127.0.0.1 only.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
from pathlib import Path
from typing import Any

from model.serving.config import ModelConfig, load_model_config

MODEL_LAYER = "application/vnd.ollama.image.model"
PROJECTOR_LAYER = "application/vnd.ollama.image.projector"
SOURCE_TAG = "qwen3.5:4b"  # installed by `ollama pull`; kept for switching back


def manifest_path(models_dir: Path, tag: str) -> Path:
    name, _, version = tag.partition(":")
    namespace, _, repo = name.rpartition("/")
    return (
        models_dir
        / "manifests"
        / "registry.ollama.ai"
        / (namespace or "library")
        / repo
        / (version or "latest")
    )


def models_dir(config: ModelConfig) -> Path:
    """Ollama's model store: configs/model.yaml, else OLLAMA_MODELS, else Ollama's default."""
    if config.ollama_models_dir:
        return Path(config.ollama_models_dir)
    if os.environ.get("OLLAMA_MODELS"):
        return Path(os.environ["OLLAMA_MODELS"])
    return Path.home() / ".ollama" / "models"


def weights_digest(manifest: dict[str, Any]) -> str:
    """The digest of the one weights layer (the projector layer is left out)."""
    layers = [x for x in manifest.get("layers", []) if x.get("mediaType") == MODEL_LAYER]
    if len(layers) != 1:
        raise ValueError(f"expected one weights layer, found {len(layers)}")
    return str(layers[0]["digest"])


def create_request(name: str, digest: str, show: dict[str, Any]) -> dict[str, Any]:
    """The /api/create body: same weights, renderer, parser, template and parameters."""
    body: dict[str, Any] = {
        "model": name,
        "files": {"model.gguf": digest},
        "stream": False,
    }
    for key in ("template", "renderer", "parser"):
        if show.get(key):
            body[key] = show[key]
    params: dict[str, Any] = {}
    for line in str(show.get("parameters") or "").splitlines():
        key, _, value = line.strip().partition(" ")
        if key and value:
            value = value.strip().strip('"')
            try:
                params[key] = json.loads(value)
            except json.JSONDecodeError:
                params[key] = value
    if params:
        body["parameters"] = params
    return body


def _post(config: ModelConfig, path: str, body: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(  # noqa: S310 - endpoint is 127.0.0.1 (ModelConfig)
        config.endpoint.base_url + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:  # noqa: S310
        return json.loads(resp.read())


def make_text_model(source: str, name: str, config: ModelConfig | None = None) -> dict[str, Any]:
    config = config or load_model_config()
    manifest = json.loads(manifest_path(models_dir(config), source).read_text(encoding="utf-8"))
    show = _post(config, "/api/show", {"model": source})
    body = create_request(name, weights_digest(manifest), show)
    _post(config, "/api/create", body)
    made = _post(config, "/api/show", {"model": name})
    if "vision" in (made.get("capabilities") or []):
        raise RuntimeError(f"{name} still has vision")
    return made


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default=SOURCE_TAG)
    parser.add_argument("--name", default=load_model_config().ollama_model)
    args = parser.parse_args()
    made = make_text_model(args.source, args.name)
    details = made.get("details") or {}
    print(
        f"{args.name}: capabilities {made.get('capabilities')}, "
        f"{details.get('quantization_level')}, renderer {made.get('renderer')}, "
        f"parser {made.get('parser')}"
    )


if __name__ == "__main__":
    main()
