"""Cloud mode really is cloud (owner bug report 2026-11-26).

1. Cloud mode: every model call goes to the cloud; nothing starts Ollama or loads Qwen.
2. No silent fallback: a failed call is an error with the reason, unless the owner turned
   on "Fall back to local if cloud fails" (default off).
3. Hybrid: exactly the per-agent table; only local agents start the local model.
4. A mode change applies to the next call and survives a restart.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from backend.reliability.resources import heavy_task_refusal
from integrations.adapters.pc import windows_sandbox as ws
from model.serving.cloud.budget import UsageLedger
from model.serving.cloud.dispatcher import CloudUnavailable, ModelDispatcher
from model.serving.cloud.keys import MemoryKeyStore
from model.serving.cloud.redact import Redactor
from model.serving.cloud.settings import CloudSettings, CloudSettingsStore, cloud_only_now
from model.serving.types import GenerateResult, Message
from security.audit.audit_logger import AuditLogger
from tests.model.serving.cloud.fake_server import FakeProvider, fake_provider, openai_reply

KEY = "nvapi-test-key-not-real-0005"  # noqa: S105
ACCEPTED = "2026-11-26T10:00:00+00:00"
HI = [Message(role="user", content="hi")]
ALL_AGENTS = [
    "router", "coding", "debugging", "web_research", "image_editing", "tool_builder", "echo",
    "communication", "memory_librarian", "documents_study", "file_system", "planner",
    "system_settings", "app_automation", None,
]  # fmt: skip


class Local:
    """The local model: counts calls and Ollama starts."""

    def __init__(self) -> None:
        self.calls = 0
        self.starts = 0

    def generate(self, messages: list[Message], **kwargs: Any) -> GenerateResult:
        self.calls += 1
        return GenerateResult(text="local", model="qwen")

    def ready(self) -> None:
        self.starts += 1


def _setup(
    data: Path, fake: FakeProvider | None, **settings: Any
) -> tuple[ModelDispatcher, Local, AuditLogger]:
    store = CloudSettingsStore(data)
    base = {
        "mode": "cloud",
        "profile": "nvidia",
        "profiles": {
            "nvidia": {
                "label": "NVIDIA",
                "preset": "nvidia",
                "base_url": fake.url if fake else "http://127.0.0.1:9",
                "model": "cloud-m",
            }
        },
        "warning_accepted_at": ACCEPTED,
        "monthly_budget_usd": 0,
        "request_timeout_s": 2,
    }
    store.save(CloudSettings.model_validate({**base, **settings}))
    local = Local()
    audit = AuditLogger(data / "audit.jsonl")
    d = ModelDispatcher(
        local.generate,
        settings=store,
        keys=MemoryKeyStore({"nvidia": KEY}),
        ledger=UsageLedger(data),
        redactor=Redactor(lambda p: False),
        audit=audit,
    )
    d.local_ready = local.ready
    return d, local, audit


# --- 1. Cloud mode: every call goes to the cloud, the local model is never touched -------------


def test_cloud_mode_sends_every_agent_and_unnamed_calls_to_the_cloud(tmp_path: Path) -> None:
    with fake_provider() as fake:
        fake.default = (200, openai_reply(text="cloud"))
        d, local, _ = _setup(tmp_path, fake)
        for agent in ALL_AGENTS:
            assert d.generate(HI, agent=agent).provider == "openai_compat", agent
    assert len(fake.requests) == len(ALL_AGENTS)
    assert (local.calls, local.starts) == (0, 0)


def test_cloud_mode_needs_no_resource_check_but_pause_still_counts() -> None:
    from backend.reliability.resources import ResourceSnapshot
    from backend.reliability.settings import AppSettings

    low = ResourceSnapshot.model_construct(
        free_ram_mb=100, total_ram_mb=16000, free_disk_gb=1, disk_path="C:", on_battery=True,
        battery_percent=5,
    )  # fmt: skip
    settings = AppSettings(heavy_only_when_plugged_in=True)
    assert heavy_task_refusal(low, settings) is not None  # local: refused
    assert heavy_task_refusal(low, settings, check_resources=False) is None  # cloud: runs
    assert heavy_task_refusal(low, settings, model_paused=True, check_resources=False)


def test_cloud_only_now_reads_the_saved_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HCWMA_DATA_DIR", str(tmp_path))
    assert cloud_only_now() is False  # no settings: Local only
    _setup(tmp_path, None)
    assert cloud_only_now() is True
    _setup(tmp_path, None, mode="hybrid")
    assert cloud_only_now() is False


def test_sandbox_does_not_reload_qwen_in_cloud_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HCWMA_DATA_DIR", str(tmp_path))
    _setup(tmp_path, None)
    log: list[str] = []

    class Backend:
        loaded = True

        def runtime_info(self) -> dict[str, Any]:
            return {"loaded": [1] if self.loaded else []}

        def unload(self) -> None:
            self.loaded = False
            log.append("unload")

        def load(self) -> None:
            log.append("load")

    class Queue:
        @contextmanager
        def exclusive(self) -> Any:
            yield

    with ws.model_released(Backend(), Queue()):
        pass
    assert log == ["unload"]


# --- 2. No silent fallback ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "kind", "words"),
    [
        (401, {"error": "bad key"}, "auth", "refused the API key"),
        (402, {"error": "pay"}, "credits", "no credits"),
        (403, {"detail": "Insufficient credits on this account"}, "credits", "no credits"),
        (429, {"error": "slow down"}, "rate_limit", "rate limit"),
        (500, {"error": "x"}, "server", "had an error"),
    ],
)
def test_failed_cloud_call_is_an_error_with_the_reason(
    tmp_path: Path, status: int, body: dict[str, Any], kind: str, words: str
) -> None:
    with fake_provider() as fake:
        fake.reply(body, status=status)
        d, local, audit = _setup(tmp_path, fake)
        with pytest.raises(CloudUnavailable) as err:
            d.generate(HI, agent="coding", task_id="t1")
    assert err.value.kind == kind and words in str(err.value)
    assert "cloud-m" in str(err.value) and KEY not in str(err.value)
    assert (local.calls, local.starts) == (0, 0)
    [rec] = audit.read_all()
    assert (rec.event, rec.outcome, rec.agent) == ("cloud_call", f"failed:{kind}", "coding")


def test_network_error_and_missing_key_are_errors_too(tmp_path: Path) -> None:
    d, local, _ = _setup(tmp_path, None)  # nothing listens on port 9
    with pytest.raises(CloudUnavailable) as err:
        d.generate(HI, agent="router")
    assert err.value.kind in {"network", "timeout"}
    d.keys = MemoryKeyStore()
    with pytest.raises(CloudUnavailable) as err:
        d.generate(HI, agent="router")
    assert err.value.kind == "no_key"
    assert (local.calls, local.starts) == (0, 0)


def test_fallback_is_off_by_default_and_on_only_when_the_owner_turns_it_on(tmp_path: Path) -> None:
    assert CloudSettings().fallback_to_local is False
    with fake_provider() as fake:
        fake.reply({"error": "x"}, status=429)
        d, local, _ = _setup(tmp_path, fake, fallback_to_local=True)
        result = d.generate(HI, agent="coding")
    assert result.provider == "local" and "rate limit" in (result.fallback_note or "")
    assert (local.calls, local.starts) == (1, 1)  # Ollama started only for this fallback


# --- 3. Hybrid: the per-agent table, nothing else ---------------------------------------------


def test_hybrid_follows_the_table_and_only_local_agents_start_qwen(tmp_path: Path) -> None:
    with fake_provider() as fake:
        fake.default = (200, openai_reply(text="cloud"))
        d, local, _ = _setup(
            tmp_path,
            fake,
            mode="hybrid",
            agent_overrides={"file_system": "cloud", "coding": "local"},
        )
        assert d.generate(HI, agent="router").provider == "openai_compat"
        assert d.generate(HI, agent="file_system").provider == "openai_compat"
        assert (local.calls, local.starts) == (0, 0)
        assert d.generate(HI, agent="coding").provider == "local"
        assert d.generate(HI, agent="planner").provider == "local"
    assert (local.calls, local.starts) == (2, 2)
    assert len(fake.requests) == 2


# --- 4. A change applies to the next call and survives a restart --------------------------------


def test_mode_change_applies_at_once_and_after_a_restart(tmp_path: Path) -> None:
    with fake_provider() as fake:
        fake.default = (200, openai_reply(text="cloud"))
        d, local, _ = _setup(tmp_path, fake, mode="local")
        assert d.generate(HI, agent="router").provider == "local"
        s = d.settings.load()
        d.settings.save(s.model_copy(update={"mode": "cloud"}))  # what the Model page does
        assert d.generate(HI, agent="router").provider == "openai_compat"  # same dispatcher
        restarted = ModelDispatcher(
            local.generate,
            settings=CloudSettingsStore(tmp_path),
            keys=MemoryKeyStore({"nvidia": KEY}),
            ledger=UsageLedger(tmp_path),
            redactor=Redactor(lambda p: False),
        )
        assert restarted.settings.load().mode == "cloud"
        assert restarted.generate(HI, agent="router").provider == "openai_compat"
