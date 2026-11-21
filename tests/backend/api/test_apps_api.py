"""The Apps page, adapter offers and the first-run wizard over the local API (owner decision
2026-11-21). A fake app scan; nothing is opened, installed or downloaded."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agents.router.router import RouterReply
from backend.api.app import create_app
from backend.apps_service import AppsService
from backend.config import BackendConfig
from integrations.apps import access, adapters, setup_state
from integrations.apps.catalog import RawScan, load_catalog
from memory.config import resolve_data_dir
from model.serving import Message
from security.audit.audit_logger import get_audit_logger
from tests.integrations.adapters.desktop.test_app_launch import fake_scan

TOKEN = "test-token-not-a-real-secret"  # noqa: S105
BASE_URL = "http://127.0.0.1:8765"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class _Router:
    async def handle(self, conversation: list[Message]) -> RouterReply:
        return RouterReply(task_id="t", action="answer", reply="hi", status="done")

    @asynccontextmanager
    async def factory(self) -> AsyncIterator[_Router]:
        yield self


class FakeScanner:
    def __init__(self, raw: RawScan | None = None) -> None:
        self.raw = raw or fake_scan()
        self.scans = 0

    def scan(self) -> RawScan:
        self.scans += 1
        return self.raw


@pytest.fixture
def scanner() -> FakeScanner:
    return FakeScanner()


@pytest.fixture
def client(scanner: FakeScanner) -> Iterator[TestClient]:
    service = AppsService(scanner, every_s=3600)
    app = create_app(
        handler_factory=_Router().factory, token=TOKEN, config=BackendConfig(), apps=service
    )
    with TestClient(app, base_url=BASE_URL) as c:
        deadline = time.monotonic() + 10
        while (scanner.scans < 1 or c.get("/apps/state", headers=AUTH).json()["scanning"]) and (
            time.monotonic() < deadline
        ):
            time.sleep(0.02)  # the scan the service starts by itself at start
        yield c
    service.stop()


def _app(state: dict[str, Any], name: str) -> dict[str, Any]:
    return next(a for a in state["apps"] if a["name"] == name)


def test_every_route_needs_the_token(client: TestClient) -> None:
    for method, path in [
        ("get", "/apps/state"), ("post", "/apps/scan"), ("post", "/apps/x/allow"),
        ("post", "/apps/x/revoke"), ("post", "/adapters/whatsapp"), ("get", "/setup/state"),
        ("post", "/setup/folders"), ("post", "/setup/finish"),
    ]:  # fmt: skip
        assert getattr(client, method)(path).status_code == 401, path


def test_scan_at_start_and_on_request_is_saved_per_pc(
    client: TestClient, scanner: FakeScanner
) -> None:
    r = client.post("/apps/scan", headers=AUTH)
    assert r.json()["ok"] and scanner.scans >= 1
    saved = resolve_data_dir() / "app_catalog.json"
    assert saved.exists() and json.loads(saved.read_text("utf-8"))["apps"]
    state = client.get("/apps/state", headers=AUTH).json()
    assert _app(state, "Google Chrome")["blocked"] and not _app(state, "Paint")["blocked"]
    assert _app(state, "WhatsApp")["adapter"] == "WhatsApp"
    records = [r for r in get_audit_logger().read_all() if r.event == "app_access"]
    assert any(r.tool == "scan" and r.outcome == "ok" for r in records)


def test_always_allow_and_revoke_from_the_apps_page(client: TestClient) -> None:
    client.post("/apps/scan", headers=AUTH)
    paint = _app(client.get("/apps/state", headers=AUTH).json(), "Paint")
    assert client.post(f"/apps/{paint['id']}/allow", headers=AUTH, json={}).status_code == 200
    assert access.is_always_allowed(paint["id"])
    state = client.get("/apps/state", headers=AUTH).json()
    assert [a["name"] for a in state["allowed"]] == ["Paint"] and state["owner_listed"] == [
        "WhatsApp"
    ]
    assert client.post(f"/apps/{paint['id']}/revoke", headers=AUTH).json() == {"revoked": True}
    assert not access.is_always_allowed(paint["id"])
    actions = [
        (r.tool, r.outcome) for r in get_audit_logger().read_all() if r.event == "app_access"
    ]
    assert ("always_allow", "allowed") in actions and ("revoke", "revoked") in actions


def test_a_blocked_app_cannot_be_always_allowed(client: TestClient) -> None:
    client.post("/apps/scan", headers=AUTH)
    chrome = _app(client.get("/apps/state", headers=AUTH).json(), "Google Chrome")
    r = client.post(f"/apps/{chrome['id']}/allow", headers=AUTH, json={})
    assert r.status_code == 403 and not access.is_always_allowed(chrome["id"])
    assert client.post("/apps/nope/allow", headers=AUTH, json={}).status_code == 404


def test_adapter_offer_after_a_scan_on_a_new_install(client: TestClient) -> None:
    (resolve_data_dir() / "adapters.json").write_text('{"adapters": {}}', "utf-8")  # new PC
    client.post("/apps/scan", headers=AUTH)
    offers = client.get("/apps/state", headers=AUTH).json()["offers"]
    assert offers == [
        {
            "adapter": "whatsapp",
            "app": "WhatsApp",
            "text": "WhatsApp found: enable the WhatsApp adapter?",
        }
    ]
    assert adapters.adapter_refusal("whatsapp_list_chats")  # off until the owner says yes
    r = client.post("/adapters/whatsapp", headers=AUTH, json={"on": True, "source": "offer"})
    assert r.status_code == 200 and adapters.adapter_refusal("whatsapp_list_chats") is None
    assert client.get("/apps/state", headers=AUTH).json()["offers"] == []
    assert client.post("/adapters/evil", headers=AUTH, json={"on": True}).status_code == 404


def test_no_offer_when_the_app_is_not_installed(scanner: FakeScanner, client: TestClient) -> None:
    (resolve_data_dir() / "adapters.json").write_text('{"adapters": {}}', "utf-8")
    scanner.raw = RawScan(start_apps=[a for a in fake_scan().start_apps if a.name != "WhatsApp"])
    client.post("/apps/scan", headers=AUTH)
    assert client.get("/apps/state", headers=AUTH).json()["offers"] == []
    assert load_catalog().find("WhatsApp") == []


def test_setup_state_folders_and_finish(client: TestClient, monkeypatch: pytest.MonkeyPatch,
                                        tmp_path: Path) -> None:  # fmt: skip
    one = tmp_path / "OneDrive" / "Desktop"
    one.mkdir(parents=True)
    perms = tmp_path / "permissions.yaml"
    real = Path(setup_state.DEFAULT_PERMISSIONS_PATH).read_text("utf-8")
    perms.write_text(real, "utf-8")
    monkeypatch.setattr(setup_state, "detect_user_folders", lambda: {"Desktop": str(one)})
    monkeypatch.setattr(setup_state, "DEFAULT_PERMISSIONS_PATH", perms)
    monkeypatch.setattr(setup_state.folder_status, "__defaults__", (perms,))
    monkeypatch.setattr(setup_state.approve_folder, "__defaults__", (perms, None))
    state = client.get("/setup/state", headers=AUTH).json()
    assert state["done"] is False
    assert state["folders"] == [
        {"name": "Desktop", "path": str(one.resolve()), "onedrive": True, "exists": True,
         "approved": False}
    ]  # fmt: skip
    # only a detected folder can be added
    bad = client.post("/setup/folders", headers=AUTH, json={"path": str(tmp_path)})
    assert bad.status_code == 400
    ok = client.post("/setup/folders", headers=AUTH, json={"path": str(one)})
    assert ok.status_code == 200 and ok.json()["added"] in perms.read_text("utf-8")
    assert setup_state.folder_status(perms)[0]["approved"] is True
    assert client.post("/setup/finish", headers=AUTH, json={"how": "finished"}).json()["done"]
    assert client.get("/setup/state", headers=AUTH).json()["done"] is True


def test_an_existing_install_keeps_its_adapters_a_new_one_starts_off(tmp_path: Path) -> None:
    old = tmp_path / "old"
    (old / "memory").mkdir(parents=True)
    assert setup_state.keep_adapters_of_existing_install(old) == ["whatsapp", "gmail"]
    assert adapters.is_on("whatsapp", adapters.adapters_path(old))
    new = tmp_path / "new"
    new.mkdir()
    assert setup_state.keep_adapters_of_existing_install(new) == []
    assert not adapters.is_on("whatsapp", adapters.adapters_path(new))
    # once only: a later "off" is never turned back on
    adapters.set_adapter("gmail", False, source="settings", path=adapters.adapters_path(old))
    assert setup_state.keep_adapters_of_existing_install(old) == []
    assert not adapters.is_on("gmail", adapters.adapters_path(old))


def test_the_approval_route_accepts_always_only_as_a_decision_word(client: TestClient) -> None:
    r = client.post("/approvals/nope", headers=AUTH, json={"decision": "always"})
    assert r.status_code == 404  # valid word, but there is no such card
    r = client.post("/approvals/nope", headers=AUTH, json={"decision": "forever"})
    assert r.status_code == 422
