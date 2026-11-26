"""The owner's cloud LLM settings, kept in `<data dir>/cloud.json` (no secrets in it).

Local only is the default. Changed only by the owner on the cloud page (token-protected
backend routes) or on the command line; no agent tool can read or write this file.
API keys are never here: they live in the OS credential store (`keys.py`), one per profile.

A *profile* is one provider set-up the owner keeps (NVIDIA, Groq, Anthropic, ...): base URL,
model and prices. One profile is active; switching is one click. The active profile's values
are mirrored in the top-level `provider` / `base_url` / `model` / price fields, which is what
the dispatcher reads.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from memory.config import resolve_data_dir

SETTINGS_FILE = "cloud.json"

Mode = Literal["local", "hybrid", "cloud"]
ProviderKind = Literal["openai_compat", "anthropic", "gemini"]
Target = Literal["local", "cloud"]
PROFILE_ID = r"^[a-z0-9][a-z0-9_-]{0,31}$"

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


class Preset(BaseModel):
    label: str
    provider: ProviderKind
    base_url: str  # "" = the owner types it (Custom)


# The provider list on the cloud page. A profile's id is its preset id unless the owner adds
# a second profile for the same preset.
PRESETS: dict[str, Preset] = {
    "nvidia": Preset(
        label="NVIDIA", provider="openai_compat", base_url="https://integrate.api.nvidia.com/v1"
    ),
    "groq": Preset(
        label="Groq", provider="openai_compat", base_url="https://api.groq.com/openai/v1"
    ),
    "openai": Preset(
        label="OpenAI", provider="openai_compat", base_url="https://api.openai.com/v1"
    ),
    "openrouter": Preset(
        label="OpenRouter", provider="openai_compat", base_url="https://openrouter.ai/api/v1"
    ),
    "deepseek": Preset(
        label="DeepSeek", provider="openai_compat", base_url="https://api.deepseek.com/v1"
    ),
    "anthropic": Preset(
        label="Anthropic", provider="anthropic", base_url="https://api.anthropic.com"
    ),
    "gemini": Preset(
        label="Google Gemini",
        provider="gemini",
        base_url="https://generativelanguage.googleapis.com",
    ),
    "custom": Preset(label="Custom (OpenAI-compatible)", provider="openai_compat", base_url=""),
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


def check_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    # Plain http only to this PC (tests' fake server, a local OpenAI-compatible server).
    if not value.startswith(("https://", "http://127.0.0.1:", "http://localhost:")):
        raise ValueError("the cloud base URL must start with https://")
    return value


class Profile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: str = Field(max_length=60)
    preset: str = "custom"
    provider: ProviderKind = "openai_compat"
    base_url: str
    model: str = Field(default="", max_length=120, pattern=r"^[A-Za-z0-9._:/@\-]*$")
    price_input_per_mtok: float = Field(default=0.0, ge=0, le=1000)
    price_output_per_mtok: float = Field(default=0.0, ge=0, le=1000)

    @field_validator("base_url")
    @classmethod
    def _https_only(cls, value: str) -> str:
        return check_base_url(value)


class CloudSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: Mode = "local"
    # The active profile's id; its key is stored under this id. Settings saved before profiles
    # existed get a profile named after their provider (so `openai_compat`).
    profile: str = ""
    profiles: dict[str, Profile] = Field(default_factory=dict)
    # Mirror of the active profile (what the dispatcher uses).
    provider: ProviderKind = "openai_compat"
    base_url: str = DEFAULT_BASE_URLS["openai_compat"]
    model: str = ""
    # Hybrid only: per-agent "local" / "cloud" that replaces the default above.
    agent_overrides: dict[str, Target] = Field(default_factory=dict)
    # US dollars per calendar month. 0 = unlimited when both prices are 0 (free credits,
    # nothing to count); with prices set, 0 means no cloud spending.
    monthly_budget_usd: float = Field(default=5.0, ge=0, le=10_000)
    # Owner-set prices (USD per million tokens), used for the cost display and the budget.
    price_input_per_mtok: float = Field(default=0.0, ge=0, le=1000)
    price_output_per_mtok: float = Field(default=0.0, ge=0, le=1000)
    request_timeout_s: float = Field(default=60.0, gt=0, le=600)
    # Owner decision 2026-11-26: off by default. Off: a failed cloud call is an error shown in
    # the chat (with the reason) and the local model is never loaded for it. On: the local
    # model answers instead, with a note.
    fallback_to_local: bool = False
    # When the owner clicked "I understand" on the data warning (ISO time), else None.
    warning_accepted_at: str | None = None

    @field_validator("base_url")
    @classmethod
    def _https_only(cls, value: str) -> str:
        return check_base_url(value)

    @model_validator(mode="before")
    @classmethod
    def _profiles(cls, data: Any) -> Any:
        """Old settings (no profiles) and the mirror fields are kept in step."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        profiles = dict(data.get("profiles") or {})
        active = data.get("profile") or ""
        if not profiles and data.get("model"):
            active = active or str(data.get("provider") or "openai_compat")
            profiles[active] = {
                "label": active,
                "preset": "custom",
                "provider": data.get("provider") or "openai_compat",
                "base_url": data.get("base_url") or DEFAULT_BASE_URLS["openai_compat"],
                "model": data.get("model", ""),
                "price_input_per_mtok": data.get("price_input_per_mtok", 0.0),
                "price_output_per_mtok": data.get("price_output_per_mtok", 0.0),
            }
        if active and active in profiles:
            p = profiles[active]
            p = p.model_dump() if isinstance(p, Profile) else dict(p)
            for key in (
                "provider",
                "base_url",
                "model",
                "price_input_per_mtok",
                "price_output_per_mtok",
            ):
                if key in p:
                    data[key] = p[key]
        data["profiles"] = profiles
        data["profile"] = active
        return data

    @property
    def key_id(self) -> str:
        """Where the active profile's API key is stored."""
        return self.profile or self.provider

    def budget_blocks(self, spent_usd: float) -> bool:
        if self.monthly_budget_usd > 0:
            return spent_usd >= self.monthly_budget_usd
        # 0: unlimited only when nothing is priced (free credits); else no spending at all.
        return self.price_input_per_mtok > 0 or self.price_output_per_mtok > 0

    @property
    def cloud_only(self) -> bool:
        """Cloud mode in effect: nothing may start Ollama or load Qwen ahead of a call
        (no auto-start, warm-up or reload). Only a fallback the owner turned on may load it."""
        return self.mode == "cloud" and bool(self.model) and self.warning_accepted_at is not None

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

    def with_profile(self, profile_id: str, profile: Profile, activate: bool) -> CloudSettings:
        profiles = {**self.profiles, profile_id: profile}
        data = {**self.model_dump(), "profiles": {k: v.model_dump() for k, v in profiles.items()}}
        if activate or not self.profile:
            data["profile"] = profile_id
        return CloudSettings.model_validate(data)

    def activate(self, profile_id: str) -> CloudSettings:
        if profile_id not in self.profiles:
            raise KeyError(profile_id)
        return CloudSettings.model_validate({**self.model_dump(), "profile": profile_id})

    def without_profile(self, profile_id: str) -> CloudSettings:
        profiles = {k: v for k, v in self.profiles.items() if k != profile_id}
        data = self.model_dump()
        data["profiles"] = {k: v.model_dump() for k, v in profiles.items()}
        if self.profile == profile_id:
            data["profile"] = next(iter(profiles), "")
            if not profiles:  # nothing left to use: back to Local only, no cloud model
                data.update(mode="local", model="", warning_accepted_at=None)
        return CloudSettings.model_validate(data)


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


def cloud_only_now() -> bool:
    """Cloud mode is in effect right now (read from disk: a change on the Model page applies
    at once and survives a restart). Missing or damaged settings -> False (Local only)."""
    return CloudSettingsStore().load().cloud_only
