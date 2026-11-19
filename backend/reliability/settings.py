"""The owner's reliability settings (tray toggles), kept in `<data dir>/settings.json`.

Changed only by the owner: the tray menu (through token-protected backend routes) or the
command line. No agent tool can read or write this file.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from memory.config import resolve_data_dir

SETTINGS_FILE = "settings.json"


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Heavy tasks (anything that runs Qwen) only while the laptop is on mains power.
    heavy_only_when_plugged_in: bool = False
    # Below these, new heavy tasks are refused (owner request, step 18). Config can only make
    # them stricter (higher), never turn them off.
    min_free_ram_mb: int = Field(default=1024, ge=1024, le=16 * 1024)
    min_free_disk_gb: float = Field(default=5.0, ge=5.0, le=500.0)


class SettingsStore:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.path = (data_dir or resolve_data_dir()) / SETTINGS_FILE
        self._lock = threading.Lock()

    def load(self) -> AppSettings:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return AppSettings.model_validate(data if isinstance(data, dict) else {})
        except (OSError, ValueError, ValidationError):
            return AppSettings()  # missing or damaged: the safe defaults

    def update(self, **changes: object) -> AppSettings:
        with self._lock:
            merged = {**self.load().model_dump(), **changes}
            settings = AppSettings.model_validate(merged)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(settings.model_dump(), f, indent=1)
            Path(tmp).replace(self.path)  # atomic
            return settings
