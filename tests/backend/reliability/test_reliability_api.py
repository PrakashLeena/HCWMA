"""Step 18 through the backend API: recovery at start, heavy tasks refused (the Router is
never reached), the health page, pause/resume, settings, backup, second-start "show"."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.config import BackendConfig
from backend.reliability.journal import TaskJournal, journal_path
from backend.reliability.resources import ResourceSnapshot
from backend.reliability.service import ReliabilityService
from security.audit.audit_logger import AuditLogger
from tests.backend.api.test_app import AUTH, BASE_URL, TOKEN, FakeRouter

CHAT = {"messages": [{"role": "user", "content": "hello"}]}


class FakeModel:
    def __init__(self) -> None:
        self.paused = False
        self.up = True

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def status(self) -> dict[str, Any]:
        if not self.up:
            raise ConnectionRefusedError
        return {"up": True, "version": "0.40.16", "loaded": not self.paused, "size_mb": 3400}


class Resources:
    def __init__(self) -> None:
        self.value = ResourceSnapshot(
            free_ram_mb=4000, total_ram_mb=16000, free_disk_gb=200.0, disk_path="C:\\",
            on_battery=False, battery_percent=90,
        )  # fmt: skip

    def __call__(self) -> ResourceSnapshot:
        return self.value


@pytest.fixture
def data(tmp_path: Path) -> Path:
    return tmp_path / "hcwma-data"


@pytest.fixture
def parts(data: Path, tmp_path: Path) -> tuple[ReliabilityService, FakeModel, Resources]:
    model, resources = FakeModel(), Resources()
    service = ReliabilityService(
        data_dir=data,
        audit=AuditLogger(tmp_path / "audit.jsonl"),
        model=model,  # type: ignore[arg-type]
        probes={
            "Gmail login": lambda: (True, "valid (owner account)"),
            "WhatsApp linked": lambda: (False, "WhatsApp Desktop is not running."),
        },
        snapshot=resources,
        autostart_enabled=lambda: False,
        auto_backup=False,
    )
    return service, model, resources


@pytest.fixture
def router() -> FakeRouter:
    return FakeRouter()


@pytest.fixture
def client(
    parts: tuple[ReliabilityService, FakeModel, Resources], router: FakeRouter
) -> Iterator[TestClient]:
    app = create_app(router.factory, TOKEN, BackendConfig(), reliability=parts[0])
    with TestClient(app, base_url=BASE_URL) as test_client:
        yield test_client


def test_chat_runs_when_resources_are_fine(client: TestClient, router: FakeRouter) -> None:
    assert client.post("/chat", json=CHAT, headers=AUTH).status_code == 200
    assert len(router.calls) == 1


@pytest.mark.parametrize("problem", ["ram", "disk", "battery", "paused"])
def test_heavy_task_refused_and_never_reaches_the_router(
    problem: str, client: TestClient, router: FakeRouter, parts: Any
) -> None:
    service, model, resources = parts
    if problem == "ram":
        resources.value = resources.value.model_copy(update={"free_ram_mb": 700})
    elif problem == "disk":
        resources.value = resources.value.model_copy(update={"free_disk_gb": 3.1})
    elif problem == "battery":
        service.update_settings(heavy_only_when_plugged_in=True)
        resources.value = resources.value.model_copy(update={"on_battery": True})
    else:
        assert client.post("/model/pause", headers=AUTH).json() == {"paused": True}
    response = client.post("/chat", json=CHAT, headers=AUTH)
    assert response.status_code == 503
    assert response.json()["detail"].startswith("Not started")
    assert router.calls == []
    # light things still work, so the owner can answer, stop and look
    assert client.get("/approvals", headers=AUTH).status_code == 200
    assert client.post("/stop", headers=AUTH).status_code == 200
    assert client.get("/health/details", headers=AUTH).status_code == 200


def test_health_page_reports_every_check(client: TestClient, parts: Any) -> None:
    service, _, _ = parts
    service.backup_now("test")
    report = client.get("/health/details", headers=AUTH).json()
    names = [item["name"] for item in report["items"]]
    assert names == [
        "Ollama", "Model loaded", "Cloud model", "RAM free", "Disk free", "Power", "Gmail login",
        "WhatsApp linked", "Last backup",
    ]  # fmt: skip
    by_name = {i["name"]: i for i in report["items"]}
    assert by_name["Ollama"]["ok"] is True and "0.40.16" in by_name["Ollama"]["value"]
    assert by_name["WhatsApp linked"]["ok"] is False
    assert by_name["Last backup"]["ok"] is True and "1 kept" in by_name["Last backup"]["value"]
    assert report["heavy_tasks_refused"] is None and report["start_with_windows"] is False
    assert client.get("/status").status_code == 200  # the page itself


def test_health_when_ollama_is_down(client: TestClient, parts: Any) -> None:
    parts[1].up = False
    by_name = {i["name"]: i for i in client.get("/health/details", headers=AUTH).json()["items"]}
    assert by_name["Ollama"]["ok"] is False and "not running" in by_name["Ollama"]["value"]


def test_every_new_route_needs_the_token(client: TestClient) -> None:
    for method, path in [
        ("GET", "/health/details"), ("GET", "/recovery"), ("GET", "/resources"),
        ("POST", "/model/pause"), ("POST", "/model/resume"), ("GET", "/settings"),
        ("POST", "/backup"), ("POST", "/app/show"), ("POST", "/app/show/take"),
    ]:  # fmt: skip
        assert client.request(method, path).status_code == 401, path
    response = client.post("/settings", json={"heavy_only_when_plugged_in": True})
    assert response.status_code == 401


def test_settings_pause_backup_and_show(client: TestClient, parts: Any, data: Path) -> None:
    service, model, _ = parts
    changed = client.post("/settings", json={"heavy_only_when_plugged_in": True}, headers=AUTH)
    assert changed.json()["heavy_only_when_plugged_in"] is True
    assert service.current_settings().heavy_only_when_plugged_in is True
    assert client.post("/model/pause", headers=AUTH).json() == {"paused": True} and model.paused
    assert client.post("/model/resume", headers=AUTH).json() == {"paused": False}
    name = client.post("/backup", headers=AUTH).json()["backup"]
    assert (data / "backups" / name / "manifest.json").is_file()
    assert client.post("/app/show/take", headers=AUTH).json() == {"show": False}
    client.post("/app/show", headers=AUTH)
    assert client.post("/app/show/take", headers=AUTH).json() == {"show": True}
    assert client.post("/app/show/take", headers=AUTH).json() == {"show": False}
    actions = [r.tool for r in service.audit.read_all() if r.event == "reliability"]
    assert actions == ["settings", "pause_model", "resume_model", "backup"]


def test_a_crash_before_this_start_is_reported_once_and_not_rerun(
    data: Path, tmp_path: Path, router: FakeRouter
) -> None:
    left = TaskJournal(journal_path(data))
    left.start_task("t-old", "send the weekly report to my boss")
    left.open_approval("a-old", task_id="t-old", agent="communication", tool="gmail_send")
    service = ReliabilityService(
        data_dir=data, audit=AuditLogger(tmp_path / "audit.jsonl"), model=FakeModel(),  # type: ignore[arg-type]
        probes={}, snapshot=Resources(), autostart_enabled=lambda: None, auto_backup=False,
    )  # fmt: skip
    app = create_app(router.factory, TOKEN, BackendConfig(), reliability=service)
    with TestClient(app, base_url=BASE_URL) as test_client:
        report = test_client.get("/recovery", headers=AUTH).json()
        assert report["tasks"] == ["send the weekly report to my boss"]
        assert "NOT run again" in report["message"] and "1 approval" in report["message"]
        assert test_client.get("/approvals", headers=AUTH).json() == {"pending": []}
        health = test_client.get("/health/details", headers=AUTH).json()
        assert "weekly report" in health["recovery"]
    assert router.calls == []  # nothing was sent to the Router (no re-run)
    assert service.journal.task("t-old").status == "interrupted"  # type: ignore[union-attr]


def test_the_running_backend_makes_the_daily_backup_by_itself(
    data: Path, tmp_path: Path, router: FakeRouter
) -> None:
    service = ReliabilityService(
        data_dir=data, audit=AuditLogger(tmp_path / "audit.jsonl"), model=FakeModel(),  # type: ignore[arg-type]
        probes={}, snapshot=Resources(), autostart_enabled=lambda: None,
    )  # fmt: skip
    app = create_app(router.factory, TOKEN, BackendConfig(), reliability=service)
    import time

    with TestClient(app, base_url=BASE_URL):
        deadline = time.monotonic() + 10
        while not service.backups.backups() and time.monotonic() < deadline:
            time.sleep(0.05)
    assert len(service.backups.backups()) == 1  # none existed, so one was due at start
    assert not service.backups.due()
