"""Routes for the cloud model page `/cloud` (owner decision 2026-11-26).

All need the per-install token: only the owner's own window can change the mode, save or
remove an API key or test the connection. No agent tool and no voice route reaches them.
Leaving Local only needs `accept_warning: true`, which the page sends only from the owner's
click on the warning that lists what leaves the PC. The API key is accepted here once, put
in the OS credential store, and never returned, logged or audited.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from model.serving.cloud.dispatcher import ModelDispatcher
from model.serving.cloud.keys import KeyStoreError
from model.serving.cloud.settings import (
    DATA_LEAVING,
    DEFAULT_BASE_URLS,
    HYBRID_CLOUD_AGENTS,
    HYBRID_LOCAL_AGENTS,
    CloudSettings,
    Mode,
    ProviderKind,
    Target,
)

AGENTS = sorted(HYBRID_CLOUD_AGENTS | HYBRID_LOCAL_AGENTS)


class CloudChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Mode
    provider: ProviderKind
    base_url: str = Field(max_length=300)
    model: str = Field(default="", max_length=120, pattern=r"^[A-Za-z0-9._:/@\-]*$")
    agent_overrides: dict[str, Target] = Field(default_factory=dict)
    monthly_budget_usd: float = Field(ge=0, le=10_000)
    price_input_per_mtok: float = Field(ge=0, le=1000)
    price_output_per_mtok: float = Field(ge=0, le=1000)
    request_timeout_s: float = Field(default=60.0, gt=0, le=600)
    accept_warning: bool = False


class KeyChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: ProviderKind
    # No length rule here: a validation error would echo the key back. Checked in save_key.
    key: str

    def __repr__(self) -> str:  # never show the key
        return f"KeyChange(provider={self.provider!r})"


def cloud_status(dispatcher: ModelDispatcher) -> dict[str, Any]:
    settings = dispatcher.settings.load()
    try:
        key_saved = bool(dispatcher.keys.get(settings.provider))
        key_error = ""
    except KeyStoreError as exc:
        key_saved, key_error = False, str(exc)
    month = dispatcher.ledger.this_month()
    return {
        "settings": settings.model_dump(),
        "active": settings.target_for("router") == "cloud" or settings.mode == "cloud",
        "key_saved": key_saved,
        "key_error": key_error,
        "usage": month.model_dump(),
        "budget_left_usd": round(max(0.0, settings.monthly_budget_usd - month.cost_usd), 4),
        "agents": settings.hybrid_table(AGENTS),
        "data_leaving": list(DATA_LEAVING),
        "default_base_urls": DEFAULT_BASE_URLS,
    }


def add_cloud_routes(
    app: FastAPI,
    require_token: Callable[..., None],
    dispatcher: Callable[[], ModelDispatcher],
    web_dir: Path,
    audit: Callable[[], Any],
) -> None:
    @app.get("/cloud", include_in_schema=False)
    def cloud_page() -> FileResponse:
        return FileResponse(web_dir / "cloud.html")

    @app.get("/cloud/status", dependencies=[Depends(require_token)])
    def status_route() -> dict[str, Any]:
        return cloud_status(dispatcher())

    @app.post("/cloud/settings", dependencies=[Depends(require_token)])
    def change(body: CloudChange) -> dict[str, Any]:
        d = dispatcher()
        old = d.settings.load()
        data = body.model_dump(exclude={"accept_warning"})
        unknown = set(data["agent_overrides"]) - set(AGENTS)
        if unknown:
            raise HTTPException(
                status_code=422, detail=f"unknown agent(s): {', '.join(sorted(unknown))}"
            )
        accepted = old.warning_accepted_at
        if body.mode == "local":
            accepted = None  # turning cloud on again shows the warning again
        elif old.mode == "local" or accepted is None:
            if not body.accept_warning:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="turning the cloud model on needs your click on the data warning",
                )
            accepted = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if body.mode != "local" and not body.model.strip():
            raise HTTPException(status_code=422, detail="set the cloud model name first")
        try:
            new = CloudSettings.model_validate({**data, "warning_accepted_at": accepted})
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()[0]["msg"]) from None
        d.settings.save(new)
        if new.mode != old.mode or new.provider != old.provider or new.model != old.model:
            audit().cloud_call(
                task_id="-",
                agent="owner",
                provider=new.provider,
                model=new.model,
                prompt_tokens=0,
                completion_tokens=0,
                cost_usd=0.0,
                outcome=f"mode:{new.mode}",
                duration_ms=0.0,
            )
        return cloud_status(d)

    @app.post("/cloud/key", dependencies=[Depends(require_token)])
    def save_key(body: KeyChange) -> dict[str, Any]:
        if not 8 <= len(body.key.strip()) <= 1000:
            raise HTTPException(status_code=422, detail="that does not look like an API key")
        try:
            dispatcher().keys.set(body.provider, body.key)
        except KeyStoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from None
        return {"saved": True, "provider": body.provider}

    @app.delete("/cloud/key/{provider}", dependencies=[Depends(require_token)])
    def delete_key(provider: ProviderKind) -> dict[str, Any]:
        dispatcher().keys.delete(provider)
        return {"saved": False, "provider": provider}

    @app.post("/cloud/test", dependencies=[Depends(require_token)])
    async def test_connection() -> dict[str, Any]:
        return await run_in_threadpool(dispatcher().test_connection)
