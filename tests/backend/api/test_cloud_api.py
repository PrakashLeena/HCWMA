"""Cloud page routes: the warning click, the key in the credential store only, and which
model answered in the chat reply (owner decision 2026-11-26)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

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
KEY = "test-cloud-key-not-real-0003"  # noqa: S105
NOTE = "Answered by the local model: the cloud provider's rate limit was reached."


class Router:
    async def handle(self, conversation: list[Message]) -> RouterReply:
        usage.record(
            GenerateResult(text="r", provider="openai_compat", model="cloud-m", cost_usd=0.01)
        )
        usage.record(
            GenerateResult(
                text="a",
                model="qwen",
                fallback_note=NOTE,
            )
        )
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
def client(tmp_path: Path, keys: MemoryKeyStore) -> Iterator[TestClient]:
    d = ModelDispatcher(
        lambda *a, **k: GenerateResult(text="local"),
        settings=CloudSettingsStore(tmp_path),
        keys=keys,
        ledger=UsageLedger(tmp_path),
    )
    app = create_app(
        handler_factory=Router().factory,
        token=TOKEN,
        config=BackendConfig(host="127.0.0.1", port=8765),
        cloud=d,
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        yield c


def _change(**overrides: Any) -> dict[str, Any]:
    body = {
        "mode": "hybrid",
        "provider": "openai_compat",
        "base_url": "https://api.example.test/v1",
        "model": "cloud-m",
        "agent_overrides": {"coding": "local"},
        "monthly_budget_usd": 5,
        "price_input_per_mtok": 0.5,
        "price_output_per_mtok": 1.5,
    }
    return {**body, **overrides}


def test_routes_need_the_token(client: TestClient) -> None:
    assert client.get("/cloud/status").status_code == 401
    assert client.post("/cloud/key", json={"provider": "anthropic", "key": KEY}).status_code == 401
    assert client.post("/cloud/settings", json=_change(accept_warning=True)).status_code == 401


def test_turning_cloud_on_needs_the_warning_click(client: TestClient) -> None:
    status = client.get("/cloud/status", headers=AUTH).json()
    assert status["settings"]["mode"] == "local"
    assert any("leave" in line or "Tool results" in line for line in status["data_leaving"])
    refused = client.post("/cloud/settings", json=_change(), headers=AUTH)
    assert refused.status_code == 409
    assert client.get("/cloud/status", headers=AUTH).json()["settings"]["mode"] == "local"
    ok = client.post("/cloud/settings", json=_change(accept_warning=True), headers=AUTH)
    assert ok.status_code == 200
    body = ok.json()
    assert body["settings"]["mode"] == "hybrid" and body["settings"]["warning_accepted_at"]
    assert body["agents"]["coding"] == "local" and body["agents"]["router"] == "cloud"
    # already on: other changes need no second click; back to local clears the acceptance
    assert (
        client.post("/cloud/settings", json=_change(mode="cloud"), headers=AUTH).status_code == 200
    )
    off = client.post("/cloud/settings", json=_change(mode="local"), headers=AUTH).json()
    assert off["settings"]["warning_accepted_at"] is None
    assert client.post("/cloud/settings", json=_change(), headers=AUTH).status_code == 409


def test_plain_http_to_another_host_and_unknown_agents_are_refused(client: TestClient) -> None:
    bad = client.post(
        "/cloud/settings",
        json=_change(base_url="http://evil.test", accept_warning=True),
        headers=AUTH,
    )
    assert bad.status_code == 422
    bad = client.post(
        "/cloud/settings",
        json=_change(agent_overrides={"nope": "cloud"}, accept_warning=True),
        headers=AUTH,
    )
    assert bad.status_code == 422


def test_key_goes_to_the_credential_store_and_is_never_returned(
    client: TestClient, keys: MemoryKeyStore, tmp_path: Path
) -> None:
    saved = client.post("/cloud/key", json={"provider": "openai_compat", "key": KEY}, headers=AUTH)
    assert saved.status_code == 200 and KEY not in saved.text
    assert keys.get("openai_compat") == KEY
    status = client.get("/cloud/status", headers=AUTH)
    assert status.json()["key_saved"] is True and KEY not in status.text
    short = client.post(
        "/cloud/key", json={"provider": "openai_compat", "key": "abc"}, headers=AUTH
    )
    assert short.status_code == 422 and "abc" not in short.text
    client.delete("/cloud/key/openai_compat", headers=AUTH)
    assert keys.get("openai_compat") is None
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert KEY not in f.read_text(encoding="utf-8", errors="ignore"), f


def test_chat_reply_says_which_model_answered(client: TestClient) -> None:
    body = client.post(
        "/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=AUTH
    ).json()
    assert [(m["where"], m["model"]) for m in body["models"]] == [
        ("cloud", "cloud-m"),
        ("local", "qwen"),
    ]
    assert body["notes"] == [
        "Answered by the local model: the cloud provider's rate limit was reached."
    ]


def test_cloud_page_is_served(client: TestClient) -> None:
    page = client.get("/cloud")
    assert page.status_code == 200 and "What leaves this PC" in page.text
