"""Which model answers, redaction, fallback, budget and audit (owner decision 2026-11-26)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from agents.runtime.model_queue import ModelQueue
from model.serving.cloud.budget import UsageLedger
from model.serving.cloud.dispatcher import ModelDispatcher, health_value
from model.serving.cloud.keys import MemoryKeyStore
from model.serving.cloud.redact import Redactor
from model.serving.cloud.settings import CloudSettings, CloudSettingsStore
from model.serving.types import GenerateResult, Message
from security.audit.audit_logger import AuditLogger
from tests.model.serving.cloud.fake_server import FakeProvider, fake_provider, openai_reply

KEY = "test-key-not-real-0002"  # noqa: S105
ACCEPTED = "2026-11-26T10:00:00+00:00"


class LocalModel:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, messages: list[Message], **kwargs: Any) -> GenerateResult:
        self.calls += 1
        return GenerateResult(text="local answer", model="qwen-test")


def _dispatcher(
    tmp_path: Path, fake: FakeProvider | None, *, key: str | None = KEY, **settings: Any
) -> tuple[ModelDispatcher, LocalModel, AuditLogger]:
    store = CloudSettingsStore(tmp_path)
    base = {
        "mode": "cloud",
        "provider": "openai_compat",
        "base_url": (fake.url if fake else "http://127.0.0.1:9"),
        "model": "cloud-m",
        "warning_accepted_at": ACCEPTED,
        "monthly_budget_usd": 1.0,
        "price_input_per_mtok": 1.0,
        "price_output_per_mtok": 2.0,
        "request_timeout_s": 2,
    }
    store.save(CloudSettings.model_validate({**base, **settings}))
    local = LocalModel()
    audit = AuditLogger(tmp_path / "audit.jsonl")
    d = ModelDispatcher(
        local.generate,
        settings=store,
        keys=MemoryKeyStore({"openai_compat": key} if key else {}),
        ledger=UsageLedger(tmp_path),
        redactor=Redactor(lambda p: "secret_dir" in str(p)),
        audit=audit,
    )
    return d, local, audit


HI = [Message(role="user", content="hi")]


def test_local_only_is_the_default_and_sends_nothing(tmp_path: Path) -> None:
    assert CloudSettings().mode == "local"
    with fake_provider() as fake:
        d, local, _ = _dispatcher(tmp_path, fake, mode="local")
        result = d.generate(HI, agent="router")
    assert fake.requests == [] and local.calls == 1
    assert (result.provider, result.text) == ("local", "local answer")


def test_cloud_needs_the_accepted_warning_and_a_model(tmp_path: Path) -> None:
    assert CloudSettings(mode="cloud", model="m").target_for("router") == "local"
    assert CloudSettings(mode="cloud", warning_accepted_at=ACCEPTED).target_for("router") == "local"
    assert (
        CloudSettings(mode="cloud", model="m", warning_accepted_at=ACCEPTED).target_for("x")
        == "cloud"
    )


def test_hybrid_defaults_and_overrides() -> None:
    s = CloudSettings(mode="hybrid", model="m", warning_accepted_at=ACCEPTED)
    for agent in (
        "router",
        "coding",
        "debugging",
        "web_research",
        "image_editing",
        "tool_builder",
        "echo",
    ):
        assert s.target_for(agent) == "cloud", agent
    for agent in (
        "communication",
        "memory_librarian",
        "documents_study",
        "file_system",
        "planner",
        "system_settings",
        "app_automation",
        None,
        "unknown_agent",
    ):
        assert s.target_for(agent) == "local", agent
    s = s.model_copy(update={"agent_overrides": {"coding": "local", "planner": "cloud"}})
    assert (s.target_for("coding"), s.target_for("planner")) == ("local", "cloud")


def test_cloud_call_is_counted_and_audited_without_text_or_key(tmp_path: Path) -> None:
    with fake_provider() as fake:
        fake.reply(openai_reply(text="cloud answer", usage=(1000, 500)))
        d, local, audit = _dispatcher(tmp_path, fake)
        result = d.generate(
            [Message(role="user", content="my private words")], agent="coding", task_id="t9"
        )
    assert local.calls == 0
    assert (result.provider, result.model, result.text) == (
        "openai_compat",
        "cloud-m",
        "cloud answer",
    )
    assert result.cost_usd == pytest.approx((1000 * 1 + 500 * 2) / 1e6)
    month = d.ledger.this_month()
    assert (month.calls, month.prompt_tokens, month.completion_tokens) == (1, 1000, 500)
    [rec] = audit.read_all()
    assert (rec.event, rec.agent, rec.task_id, rec.outcome) == ("cloud_call", "coding", "t9", "ok")
    assert rec.args["provider"] == "openai_compat" and rec.args["model"] == "cloud-m"
    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "private words" not in raw and KEY not in raw and "cloud answer" not in raw
    assert KEY not in (tmp_path / "cloud.json").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("status", "kind"), [(429, "rate limit"), (500, "error"), (401, "refused")]
)
def test_provider_errors_fall_back_to_local_with_a_note(
    tmp_path: Path, status: int, kind: str
) -> None:
    with fake_provider() as fake:
        fake.reply({"error": "x"}, status=status)
        d, local, audit = _dispatcher(tmp_path, fake)
        result = d.generate(HI, agent="router")
    assert local.calls == 1 and result.provider == "local"
    assert result.fallback_note and kind in result.fallback_note
    assert d.ledger.this_month().fallbacks == 1
    assert audit.read_all()[0].outcome.startswith("fallback:")


def test_network_error_falls_back(tmp_path: Path) -> None:
    d, local, _ = _dispatcher(tmp_path, None)
    result = d.generate(HI, agent="router")
    assert local.calls == 1 and "local model" in (result.fallback_note or "")


def test_no_key_falls_back_without_a_request(tmp_path: Path) -> None:
    with fake_provider() as fake:
        d, local, _ = _dispatcher(tmp_path, fake, key=None)
        result = d.generate(HI, agent="router")
    assert fake.requests == [] and local.calls == 1
    assert "no cloud API key" in (result.fallback_note or "")


def test_budget_reached_stops_cloud_calls(tmp_path: Path) -> None:
    with fake_provider() as fake:
        # 400k in + 300k out at $1 / $2 per million = $1.00 = the whole budget
        fake.reply(openai_reply(text="first", usage=(400_000, 300_000)))
        d, local, _ = _dispatcher(tmp_path, fake)
        assert d.generate(HI, agent="router").provider == "openai_compat"
        second = d.generate(HI, agent="router")
    assert len(fake.requests) == 1 and local.calls == 1
    assert "budget" in (second.fallback_note or "")
    ok, value = health_value(d)
    assert ok is False and "$1.00 of $1.00" in value


def test_a_new_month_starts_from_zero(tmp_path: Path) -> None:
    now = [datetime(2026, 11, 30)]
    ledger = UsageLedger(tmp_path, clock=lambda: now[0])
    ledger.add(prompt_tokens=10, completion_tokens=10, cost_usd=5.0)
    now[0] = datetime(2026, 12, 1)
    assert ledger.this_month().cost_usd == 0
    assert set(ledger.history()) == {"2026-11"}


def test_what_is_sent_is_redacted(tmp_path: Path) -> None:
    card = "4111 1111 1111 1111"
    messages = [
        Message(role="system", content="rules"),
        Message(
            role="user",
            content=f"my key is sk-proj-abcdefghijklmnopqrstuv and card {card}, password: hunter22",
        ),
        Message(
            role="tool",
            content="C:/secret_dir/id_rsa\n-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA",
            tool_name="read_file",
        ),
        Message(role="tool", content="see C:/Users/me/notes.txt", tool_name="search_files"),
    ]
    with fake_provider() as fake:
        fake.reply(openai_reply(text="ok"))
        d, _, _ = _dispatcher(tmp_path, fake)
        d.generate(messages, agent="file_system")
    raw = fake.last["raw"]
    for secret in (
        "sk-proj-abcdefghijklmnopqrstuv",
        "4111 1111 1111 1111",
        "hunter22",
        "secret_dir",
        "OPENSSH",
    ):
        assert secret not in raw, secret
    assert "withheld" in raw
    assert "C:/Users/me/notes.txt" in raw  # ordinary paths are kept


def test_a_bad_tool_call_from_the_cloud_falls_back(tmp_path: Path) -> None:
    with fake_provider() as fake:
        fake.reply(openai_reply(calls=[("search_files", "[1, 2]")]))
        d, local, _ = _dispatcher(tmp_path, fake)
        result = d.generate(HI, agent="router")
    assert local.calls == 1 and "could not be read" in (result.fallback_note or "")


def test_queue_passes_the_agent_only_to_a_backend_that_takes_it(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def plain(messages: list[Message], tools: Any = None, max_tokens: int = 512) -> GenerateResult:
        return GenerateResult(text="x")

    def aware(messages: list[Message], *, agent: str | None = None, **kw: Any) -> GenerateResult:
        seen.append({"agent": agent, **kw})
        return GenerateResult(text="y")

    assert ModelQueue(plain).generate(HI, agent="router").text == "x"
    ModelQueue(aware).generate(HI, agent="router", task_id="t1")
    assert seen[0]["agent"] == "router" and seen[0]["task_id"] == "t1"


def test_test_connection(tmp_path: Path) -> None:
    with fake_provider() as fake:
        fake.reply(openai_reply(text="OK"))
        d, _, _ = _dispatcher(tmp_path, fake, mode="local")
        assert d.test_connection()["ok"] is True
        fake.reply({"error": "x"}, status=401)
        r = d.test_connection()
    assert r["ok"] is False and r["kind"] == "auth"
    d2, _, _ = _dispatcher(tmp_path / "b", None, key=None)
    assert d2.test_connection()["ok"] is False


def test_a_zero_budget_with_zero_prices_is_unlimited(tmp_path: Path) -> None:
    with fake_provider() as fake:
        fake.reply(openai_reply(text="free", usage=(5000, 5000)))
        d, local, _ = _dispatcher(
            tmp_path, fake, monthly_budget_usd=0, price_input_per_mtok=0, price_output_per_mtok=0
        )
        result = d.generate(HI, agent="router")
    assert result.provider == "openai_compat" and local.calls == 0
    assert "unlimited" in health_value(d)[1]


def test_a_zero_budget_with_prices_allows_no_spending(tmp_path: Path) -> None:
    with fake_provider() as fake:
        d, local, _ = _dispatcher(tmp_path, fake, monthly_budget_usd=0)
        result = d.generate(HI, agent="router")
    assert fake.requests == [] and local.calls == 1
    assert "budget" in (result.fallback_note or "")


def test_old_settings_become_a_profile(tmp_path: Path) -> None:
    from model.serving.cloud.settings import CloudSettings

    s = CloudSettings.model_validate(
        {
            "provider": "openai_compat",
            "base_url": "https://integrate.api.nvidia.com/v1",
            "model": "m",
        }
    )
    assert s.profile == "openai_compat" and s.key_id == "openai_compat"
    assert s.profiles["openai_compat"].base_url == "https://integrate.api.nvidia.com/v1"
