"""Model page routes: the warning click, provider profiles, keys in the credential store only
(read back, never echoed), model list, and which model answered (owner decisions 2026-11-26)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agents.router.router import RouterReply
from backend.api.app import create_app
from backend.config import BackendConfig
from model.serving import GenerateResult, Message, usage
from model.serving.cloud.budget import UsageLedger
from model.serving.cloud.dispatcher import ModelDispatcher
from model.serving.cloud.keys import MemoryKeyStore
from model.serving.cloud.settings import CloudSettingsStore

TOKEN = "test-token-not-a-real-secret"  # noqa: S105
AUTH = {"Authorization": f"Bearer {TOKEN}"}
KEY = "nvapi-test-cloud-key-not-real-0003"  # noqa: S105
GROQ_KEY = "gsk_testgroqkeynotreal0004"  # noqa: S105
NOTE = "Answered by the local model: the cloud provider's rate limit was reached."


class Router:
    async def handle(self, conversation: list[Message]) -> RouterReply:
        usage.record(
            GenerateResult(text="r", provider="openai_compat", model="cloud-m", cost_usd=0.01)
        )
        usage.record(GenerateResult(text="a", model="qwen", fallback_note=NOTE))
        return RouterReply(
            task_id="t", action="answer", reply="hi", status="done", agents=[], tool_calls=0
        )

    @asynccontextmanager
    async def factory(self) -> AsyncIterator[Router]:
        yield self


@pytest.fixture
def keys() -> MemoryKeyStore:
    return MemoryKeyStore()


@pytest.fixture
def dispatcher(tmp_path: Path, keys: MemoryKeyStore) -> ModelDispatcher:
    return ModelDispatcher(
        lambda *a, **k: GenerateResult(text="local"),
        settings=CloudSettingsStore(tmp_path),
        keys=keys,
        ledger=UsageLedger(tmp_path),
    )


@pytest.fixture
def client(dispatcher: ModelDispatcher) -> Iterator[TestClient]:
    app = create_app(
        handler_factory=Router().factory,
        token=TOKEN,
        config=BackendConfig(host="127.0.0.1", port=8765),
        cloud=dispatcher,
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        yield c


def _profile(client: TestClient, preset: str = "nvidia", **kw: Any) -> dict[str, Any]:
    r = client.post("/cloud/profiles", json={"preset": preset, "model": "m-1", **kw}, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def _mode(mode: str = "hybrid", **kw: Any) -> dict[str, Any]:
    return {"mode": mode, "agent_overrides": {}, "monthly_budget_usd": 0, **kw}


def test_routes_need_the_token(client: TestClient) -> None:
    assert client.get("/cloud/status").status_code == 401
    assert client.post("/cloud/profiles", json={"preset": "nvidia"}).status_code == 401
    assert client.post("/cloud/profiles/nvidia/key", json={"key": KEY}).status_code == 401
    assert client.get("/cloud/profiles/nvidia/models").status_code == 401
    assert client.post("/cloud/settings", json=_mode(accept_warning=True)).status_code == 401


def test_presets_cover_the_providers(client: TestClient) -> None:
    presets = client.get("/cloud/status", headers=AUTH).json()["presets"]
    assert presets["nvidia"]["base_url"] == "https://integrate.api.nvidia.com/v1"
    assert {
        "nvidia",
        "groq",
        "openai",
        "openrouter",
        "deepseek",
        "anthropic",
        "gemini",
        "custom",
    } <= set(presets)
    assert presets["anthropic"]["provider"] == "anthropic"
    assert presets["deepseek"]["provider"] == "openai_compat"


def test_turning_cloud_on_needs_the_warning_click(client: TestClient) -> None:
    status = client.get("/cloud/status", headers=AUTH).json()
    assert status["settings"]["mode"] == "local"
    assert (
        client.post("/cloud/settings", json=_mode(accept_warning=True), headers=AUTH).status_code
        == 422
    )
    _profile(client)  # a model is needed before cloud can be on
    refused = client.post("/cloud/settings", json=_mode(), headers=AUTH)
    assert refused.status_code == 409
    assert client.get("/cloud/status", headers=AUTH).json()["settings"]["mode"] == "local"
    ok = client.post(
        "/cloud/settings",
        json=_mode(agent_overrides={"coding": "local"}, accept_warning=True),
        headers=AUTH,
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["settings"]["mode"] == "hybrid" and body["settings"]["warning_accepted_at"]
    assert body["agents"]["coding"] == "local" and body["agents"]["router"] == "cloud"
    assert client.post("/cloud/settings", json=_mode("cloud"), headers=AUTH).status_code == 200
    off = client.post("/cloud/settings", json=_mode("local"), headers=AUTH).json()
    assert off["settings"]["warning_accepted_at"] is None
    assert client.post("/cloud/settings", json=_mode(), headers=AUTH).status_code == 409


def test_custom_needs_https_and_unknown_things_are_refused(client: TestClient) -> None:
    bad = client.post(
        "/cloud/profiles", json={"preset": "custom", "base_url": "http://evil.test"}, headers=AUTH
    )
    assert bad.status_code == 422
    assert client.post("/cloud/profiles", json={"preset": "nope"}, headers=AUTH).status_code == 422
    body = _profile(client, "custom", base_url="https://llm.example.test/v1")
    assert body["profiles"][0]["base_url"] == "https://llm.example.test/v1"
    bad = client.post(
        "/cloud/settings",
        json=_mode(agent_overrides={"nope": "cloud"}, accept_warning=True),
        headers=AUTH,
    )
    assert bad.status_code == 422


def test_key_is_saved_read_back_and_only_its_end_is_shown(
    client: TestClient, keys: MemoryKeyStore, tmp_path: Path
) -> None:
    _profile(client)
    saved = client.post("/cloud/profiles/nvidia/key", json={"key": f"Bearer {KEY}"}, headers=AUTH)
    assert saved.status_code == 200
    assert saved.json() == {"saved": True, "profile": "nvidia", "key_hint": "…0003"}
    assert KEY not in saved.text
    assert keys.get("nvidia") == KEY  # "Bearer " dropped
    status = client.get("/cloud/status", headers=AUTH)
    assert KEY not in status.text
    [p] = status.json()["profiles"]
    assert (p["key_saved"], p["key_hint"]) == (True, "…0003")
    for bad in ("abc", "has a space inside it", ""):
        r = client.post("/cloud/profiles/nvidia/key", json={"key": bad}, headers=AUTH)
        assert r.status_code == 422 and (not bad or bad not in r.text)
    client.delete("/cloud/profiles/nvidia/key", headers=AUTH)
    assert keys.get("nvidia") is None
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert KEY not in f.read_text(encoding="utf-8", errors="ignore"), f


def test_a_key_that_does_not_read_back_is_not_reported_saved(
    client: TestClient, keys: MemoryKeyStore
) -> None:
    _profile(client)
    keys.lose_writes = True  # the store says nothing and keeps nothing (the reported bug)
    r = client.post("/cloud/profiles/nvidia/key", json={"key": KEY}, headers=AUTH)
    assert r.status_code == 422 and "read back" in r.json()["detail"]
    assert client.get("/cloud/status", headers=AUTH).json()["profiles"][0]["key_saved"] is False


def test_a_key_needs_a_saved_profile(client: TestClient) -> None:
    r = client.post("/cloud/profiles/groq/key", json={"key": GROQ_KEY}, headers=AUTH)
    assert r.status_code == 404


def test_profiles_keep_their_own_keys_and_switch_in_one_click(
    client: TestClient, keys: MemoryKeyStore, dispatcher: ModelDispatcher
) -> None:
    _profile(client, "nvidia", model="mistralai/m")
    _profile(client, "groq", model="llama-x", activate=False)
    client.post("/cloud/profiles/nvidia/key", json={"key": KEY}, headers=AUTH)
    client.post("/cloud/profiles/groq/key", json={"key": GROQ_KEY}, headers=AUTH)
    s = dispatcher.settings.load()
    assert (s.profile, s.model, s.base_url) == (
        "nvidia",
        "mistralai/m",
        "https://integrate.api.nvidia.com/v1",
    )
    body = client.post("/cloud/profiles/groq/activate", headers=AUTH).json()
    assert [p["id"] for p in body["profiles"] if p["active"]] == ["groq"]
    s = dispatcher.settings.load()
    assert (s.profile, s.model, s.base_url, s.key_id) == (
        "groq",
        "llama-x",
        "https://api.groq.com/openai/v1",
        "groq",
    )
    assert keys.get(s.key_id) == GROQ_KEY and keys.get("nvidia") == KEY
    assert client.post("/cloud/profiles/nope/activate", headers=AUTH).status_code == 404
    # removing the active profile removes its key and moves to the next one
    body = client.delete("/cloud/profiles/groq", headers=AUTH).json()
    assert keys.get("groq") is None and body["settings"]["profile"] == "nvidia"
    body = client.delete("/cloud/profiles/nvidia", headers=AUTH).json()
    assert body["settings"]["mode"] == "local" and body["profiles"] == []


def test_model_list_is_fetched_with_the_saved_key(
    client: TestClient, dispatcher: ModelDispatcher
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "z-ai/glm"}, {"id": "a/b"}]})

    dispatcher.transport = httpx.MockTransport(handler)
    _profile(client)
    client.post("/cloud/profiles/nvidia/key", json={"key": KEY}, headers=AUTH)
    r = client.get("/cloud/profiles/nvidia/models", headers=AUTH)
    assert r.json() == {"profile": "nvidia", "models": ["a/b", "z-ai/glm"]}
    assert str(seen[0].url) == "https://integrate.api.nvidia.com/v1/models"
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert KEY not in r.text
    dispatcher.transport = httpx.MockTransport(lambda req: httpx.Response(401, json={}))
    r = client.get("/cloud/profiles/nvidia/models", headers=AUTH)
    assert r.status_code == 502 and "refused the API key" in r.json()["detail"]


def test_chat_reply_says_which_model_answered(client: TestClient) -> None:
    body = client.post(
        "/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=AUTH
    ).json()
    assert [(m["where"], m["model"]) for m in body["models"]] == [
        ("cloud", "cloud-m"),
        ("local", "qwen"),
    ]
    assert body["notes"] == [NOTE]


def test_cloud_page_is_served(client: TestClient) -> None:
    page = client.get("/cloud")
    assert page.status_code == 200 and "What leaves this PC" in page.text
