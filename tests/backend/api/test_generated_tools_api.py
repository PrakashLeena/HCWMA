"""Step 13: the owner's review screen over the local API (token only, full code shown)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.config import BackendConfig
from integrations.adapters.local_data.tool_builder_tools import (
    NameInput,
    ScaffoldInput,
    ToolBuilderDeps,
    _run_tool_tests,
    _scaffold,
    _submit,
)
from integrations.generated_tools.review import ReviewService
from integrations.generated_tools.store import GeneratedStore
from security.audit.audit_logger import AuditLogger
from tests.integrations.generated_tools import samples
from tests.integrations.generated_tools.test_lifecycle import FakeSandbox

TOKEN = "test-token-not-a-real-secret"  # noqa: S105
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@asynccontextmanager
async def _factory() -> AsyncIterator[Any]:
    yield object()


def _client(tmp_path: Path) -> tuple[TestClient, ToolBuilderDeps]:
    audit = AuditLogger(tmp_path / "audit.jsonl")
    deps = ToolBuilderDeps(
        audit=lambda: audit,
        sandbox=lambda: FakeSandbox(),
        reserved=lambda: frozenset(),
        catalog_lines=lambda: [],
    )
    review = ReviewService(GeneratedStore(), audit, ["echo", "file_system"])
    app = create_app(_factory, TOKEN, BackendConfig(), review=review)
    return TestClient(app, base_url="http://127.0.0.1:8765"), deps


def test_every_review_route_needs_the_token(tmp_path: Path) -> None:
    with _client(tmp_path)[0] as client:
        for method, path in [
            ("get", "/generated-tools"),
            ("get", "/generated-tools/word_count"),
            ("post", "/generated-tools/word_count/approve"),
            ("post", "/generated-tools/word_count/disable"),
            ("post", "/generated-tools/word_count/enable"),
            ("post", "/generated-tools/word_count/rollback"),
        ]:
            assert getattr(client, method)(path).status_code == 401, path


def test_review_screen_then_approve_with_the_exact_hash(tmp_path: Path) -> None:
    client, deps = _client(tmp_path)
    with client:
        _scaffold(
            deps,
            ScaffoldInput(
                name="word_count",
                manifest_yaml=samples.MANIFEST,
                adapter_code=samples.ADAPTER,
                test_code=samples.TESTS,
            ),
        )
        listing = client.get("/generated-tools", headers=AUTH).json()["tools"]
        assert [(t["name"], t["status"]) for t in listing] == [("word_count", "quarantined")]

        review = client.get("/generated-tools/word_count", headers=AUTH).json()
        assert review["adapter_code"] == samples.ADAPTER  # the full code
        assert "Count the words" in review["summary"]  # the plain summary
        assert review["permissions"]["network"] == []  # the permissions

        body = {"hash": review["hash"], "agents": ["echo"]}
        refused = client.post("/generated-tools/word_count/approve", json=body, headers=AUTH)
        assert refused.status_code == 409 and "tests have not passed" in refused.json()["detail"]

        _run_tool_tests(deps, NameInput(name="word_count"))
        _submit(deps, NameInput(name="word_count"))
        stale = client.post(
            "/generated-tools/word_count/approve",
            json={"hash": "0" * 64, "agents": ["echo"]},
            headers=AUTH,
        )
        assert stale.status_code == 409
        approved = client.post("/generated-tools/word_count/approve", json=body, headers=AUTH)
        assert approved.status_code == 200 and approved.json()["status"] == "approved"
        off = client.post("/generated-tools/word_count/disable", headers=AUTH)
        assert off.json()["status"] == "disabled"
        assert client.get("/generated-tools/nope", headers=AUTH).status_code == 404
        assert client.get("/tools").status_code == 200  # the page itself


def test_review_page_shows_text_without_html(tmp_path: Path) -> None:
    source = (Path(__file__).resolve().parents[3] / "app" / "web" / "tools.js").read_text(
        encoding="utf-8"
    )
    assert "innerHTML" not in source and "insertAdjacentHTML" not in source
