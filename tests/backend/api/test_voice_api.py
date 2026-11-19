"""Step 14: push-to-talk over the local API, and a spoken "yes" never approving anything."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.config import BackendConfig
from security.audit.audit_logger import AuditLogger
from tests.backend.api.test_approvals_api import (
    AUTH,
    BASE_URL,
    TOKEN,
    ApprovingRouter,
    _chat_in_background,
    _finish,
    _wait_for_prompt,
)
from tests.voice.fakes import FakeMic, FakeStt, make_service
from voice.service import VoiceService


@pytest.fixture
def router(tmp_path: Path) -> ApprovingRouter:
    return ApprovingRouter(AuditLogger(tmp_path / "audit.jsonl"))


@pytest.fixture
def parts(tmp_path: Path) -> tuple[VoiceService, FakeMic, FakeStt]:
    return make_service(tmp_path, stt=FakeStt(text="yes, approve it"))


@pytest.fixture
def client(
    router: ApprovingRouter, parts: tuple[VoiceService, FakeMic, FakeStt]
) -> Iterator[TestClient]:
    app = create_app(
        handler_factory=router.factory, token=TOKEN, config=BackendConfig(), voice=parts[0]
    )
    with TestClient(app, base_url=BASE_URL) as c:
        yield c


def test_voice_routes_need_the_token(
    client: TestClient, parts: tuple[VoiceService, FakeMic, FakeStt]
) -> None:
    for method, path in [
        ("get", "/voice"),
        ("post", "/voice/start"),
        ("post", "/voice/stop"),
        ("post", "/voice/speak"),
        ("post", "/voice/quiet"),
        ("post", "/voice/cancel"),
    ]:
        assert getattr(client, method)(path).status_code == 401
    assert parts[1].opened == 0


def test_push_to_talk_round_trip(
    client: TestClient, parts: tuple[VoiceService, FakeMic, FakeStt]
) -> None:
    _, mic, stt = parts
    info = client.get("/voice", headers=AUTH).json()
    assert info["available"] and info["stt_model"] == "small" and not info["listening"]
    assert client.post("/voice/start", json={"language": "ta"}, headers=AUTH).json()["listening"]
    heard = client.post("/voice/stop", headers=AUTH).json()
    assert heard["text"] == "yes, approve it" and heard["language"] == "ta"
    assert not mic.open_now and not stt.loaded


def test_unknown_language_is_rejected(client: TestClient) -> None:
    assert client.post("/voice/start", json={"language": "fr"}, headers=AUTH).status_code == 422


def test_stop_everything_closes_the_microphone(
    client: TestClient, parts: tuple[VoiceService, FakeMic, FakeStt]
) -> None:
    client.post("/voice/start", json={"language": "en"}, headers=AUTH)
    client.post("/stop", headers=AUTH)
    assert not parts[1].open_now and not parts[2].loaded


def test_a_spoken_yes_never_approves(
    client: TestClient, router: ApprovingRouter, parts: tuple[VoiceService, FakeMic, FakeStt]
) -> None:
    thread, box = _chat_in_background(client)
    prompt = _wait_for_prompt(client)

    # The owner holds the talk button and says "yes, approve it".
    client.post("/voice/start", json={"language": "en"}, headers=AUTH)
    heard = client.post("/voice/stop", headers=AUTH).json()["text"]
    client.post("/voice/speak", json={"text": heard}, headers=AUTH)

    pending = client.get("/approvals", headers=AUTH).json()["pending"]
    assert [p["id"] for p in pending] == [prompt["id"]]  # still waiting for a click
    assert router.ran == []

    # Only the click on the card decides it.
    client.post(f"/approvals/{prompt['id']}", json={"decision": "deny"}, headers=AUTH)
    assert _finish(thread, box)["reply"] == "denied" and router.ran == []


def test_without_a_voice_service_the_routes_say_so(router: ApprovingRouter) -> None:
    app = create_app(handler_factory=router.factory, token=TOKEN, config=BackendConfig())
    with TestClient(app, base_url=BASE_URL) as c:
        assert c.get("/voice", headers=AUTH).status_code == 404
        assert c.post("/stop", headers=AUTH).status_code == 200
