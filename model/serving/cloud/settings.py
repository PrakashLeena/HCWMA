"""The owner's cloud LLM settings, kept in `<data dir>/cloud.json` (no secrets in it).

Local only is the default. Changed only by the owner on the cloud page (token-protected
backend routes) or on the command line; no agent tool can read or write this file.
The API key is never here: it lives in the OS credential store (`keys.py`).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from memory.config import resolve_data_dir

SETTINGS_FILE = "cloud.json"

Mode = Literal["local", "hybrid", "cloud"]
ProviderKind = Literal["openai_compat", "anthropic", "gemini"]
Target = Literal["local", "cloud"]

# Owner decision 2026-11-26: Hybrid sends these agents to the cloud, the rest stay local.
HYBRID_CLOUD_AGENTS: frozenset[str] = frozenset(
    {"router", "coding", "debugging", "web_research", "image_editing", "tool_builder", "echo"}
)
HYBRID_LOCAL_AGENTS: frozenset[str] = frozenset(
    {
        "communication",
        "memory_librarian",
        "documents_study",
        "file_system",
        "planner",
        "system_settings",
        "app_automation",
    }
)

DEFAULT_BASE_URLS: dict[ProviderKind, str] = {
    "openai_compat": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
}

# What leaves the PC when cloud is on. Shown before the owner enables it (and on the page).
DATA_LEAVING = (
    "The text of your chat requests and the recent conversation for every agent that runs in "
    "the cloud.",
    "Each such agent's instructions and the list of tools it may use (names and argument shapes).",
    "Tool results those agents read: file names and folder listings, file and document text, "
    "web page text, window contents, image details, code and error output.",
    "Things HCWMA remembers about you that are given to those agents.",
    "Protected-path names and contents, and anything that looks like a key, token, password "
    "or card number, are replaced with [redacted] before sending, but redaction is a pattern "
    "match and can miss things.",
    "The provider's own terms decide how long it keeps what it receives.",
)


class CloudSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: Mode = "local"
    provider: ProviderKind = "openai_compat"
    base_url: str = DEFAULT_BASE_URLS["openai_compat"]
    model: str = ""
    # Hybrid only: per-agent "local" / "cloud" that replaces the default above.
    agent_overrides: dict[str, Target] = Field(default_factory=dict)
    # Budget in US dollars per calendar month; 0 = no cloud calls at all.
    monthly_budget_usd: float = Field(default=5.0, ge=0, le=10_000)
    # Owner-set prices (USD per million tokens), used for the cost display and the budget.
    price_input_per_mtok: float = Field(default=0.0, ge=0, le=1000)
    price_output_per_mtok: float = Field(default=0.0, ge=0, le=1000)
    request_timeout_s: float = Field(default=60.0, gt=0, le=600)
    # When the owner clicked "I understand" on the data warning (ISO time), else None.
    warning_accepted_at: str | None = None

    @field_validator("base_url")
    @classmethod
    def _https_only(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        # Plain http only to this PC (tests' fake server, a local OpenAI-compatible server).
        if not value.startswith(("https://", "http://127.0.0.1:", "http://localhost:")):
            raise ValueError("the cloud base URL must start with https://")
        return value

    def target_for(self, agent: str | None) -> Target:
        """Where a model call for `agent` runs. Unknown or missing agent -> the safe side."""
        if self.mode == "local" or not self.model or self.warning_accepted_at is None:
            return "local"
        if self.mode == "cloud":
            return "cloud"
        if agent is None:
            return "local"
        if agent in self.agent_overrides:
            return self.agent_overrides[agent]
        return "cloud" if agent in HYBRID_CLOUD_AGENTS else "local"

    def hybrid_table(self, agents: list[str]) -> dict[str, Target]:
        out: dict[str, Target] = {}
        for agent in agents:
            default: Target = "cloud" if agent in HYBRID_CLOUD_AGENTS else "local"
            out[agent] = self.agent_overrides.get(agent, default)
        return out


class CloudSettingsStore:
    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = data_dir
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return (self._data_dir or resolve_data_dir()) / SETTINGS_FILE

    def load(self) -> CloudSettings:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return CloudSettings.model_validate(data if isinstance(data, dict) else {})
        except (OSError, ValueError, ValidationError):
            return CloudSettings()  # missing or damaged: Local only

    def save(self, settings: CloudSettings) -> CloudSettings:
        with self._lock:
            path = self.path
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(settings.model_dump(), f, indent=1)
            Path(tmp).replace(path)  # atomic
            return settings
