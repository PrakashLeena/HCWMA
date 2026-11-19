"""Load and validate `configs/voice.yaml` (build step 14)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / "configs" / "voice.yaml"
DEBUG_AUDIO_DIR = REPO_ROOT / "workspace" / "voice_debug"

Language = Literal["en", "ta", "si"]
LANGUAGE_NAMES: dict[str, str] = {"en": "English", "ta": "Tamil", "si": "Sinhala"}


class VoiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stt_model: Literal["tiny", "base", "small"] = "small"
    models_dir: Path = Path("workspace/voice_models")
    max_record_seconds: float = Field(default=30, gt=0, le=120)
    max_transcribe_seconds: float = Field(default=40, gt=0, le=300)
    stt_ram_mb: int = Field(default=1100, ge=0)
    speak_max_chars: int = Field(default=1200, ge=50, le=10_000)
    debug_save_audio: bool = False

    def models_path(self) -> Path:
        p = self.models_dir
        return p if p.is_absolute() else REPO_ROOT / p


def load_voice_config(path: Path | None = None) -> VoiceConfig:
    with (path or DEFAULT_CONFIG_PATH).open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return VoiceConfig.model_validate(data)
