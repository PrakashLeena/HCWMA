"""Routes for the cloud model page `/cloud` (owner decision 2026-11-26).

All need the per-install token: only the owner's own window (on 127.0.0.1) can change the
mode, add or switch a provider profile, save or remove an API key, list models or test the
connection. No agent tool and no voice route reaches them.

- Leaving Local only needs `accept_warning: true`, which the page sends only from the
  owner's click on the warning that lists what leaves the PC.
- An API key is accepted once, put in the OS credential store, read back, and never
  returned, logged or audited: the page only ever gets its last 4 characters.
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

from model.serving.cloud.dispatcher import ModelDispatcher, list_models
from model.serving.cloud.keys import KeyStoreError, key_hint
from model.serving.cloud.providers import CloudError
from model.serving.cloud.settings import (
    DATA_LEAVING,
    HYBRID_CLOUD_AGENTS,
    HYBRID_LOCAL_AGENTS,
    PRESETS,
    PROFILE_ID,
    CloudSettings,
    Mode,
    Profile,
    Target,
)

AGENTS = sorted(HYBRID_CLOUD_AGENTS | HYBRID_LOCAL_AGENTS)


class CloudChange(BaseModel):
    """Mode, budget and the Hybrid table. The provider set-up lives in profiles."""

    model_config = ConfigDict(extra="forbid")

    mode: Mode
    agent_overrides: dict[str, Target] = Field(default_factory=dict)
    monthly_budget_usd: float = Field(ge=0, le=10_000)
    request_timeout_s: float = Field(default=60.0, gt=0, le=600)
    accept_warning: bool = False


class ProfileChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset: str = Field(max_length=20)
    label: str | None = Field(default=None, max_length=60)
    base_url: str | None = Field(default=None, max_length=300)
    model: str = Field(default="", max_length=120, pattern=r"^[A-Za-z0-9._:/@\-]*$")
    price_input_per_mtok: float = Field(default=0.0, ge=0, le=1000)
    price_output_per_mtok: float = Field(default=0.0, ge=0, le=1000)
    activate: bool = True


class KeyChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # No length rule here: a validation error would echo the key back. Checked by the store.
    key: str

    def __repr__(self) -> str:  # never show the key
        return "KeyChange(<hidden>)"


def _hint(d: ModelDispatcher, profile_id: str) -> tuple[str | None, str]:
    try:
        return key_hint(d.keys.get(profile_id)), ""
    except KeyStoreError as exc:
        return None, str(exc)


def cloud_status(d: ModelDispatcher) -> dict[str, Any]:
    settings = d.settings.load()
    month = d.ledger.this_month()
    profiles = []
    for pid, p in settings.profiles.items():
        hint, error = _hint(d, pid)
        profiles.append(
            {
                "id": pid,
                **p.model_dump(),
                "active": pid == settings.profile,
                "key_saved": hint is not None,
                "key_hint": hint,
                "key_error": error,
            }
        )
    unlimited = settings.monthly_budget_usd == 0 and not settings.budget_blocks(0)
    return {
        "settings": settings.model_dump(exclude={"profiles"}),
        "profiles": profiles,
        "presets": {k: v.model_dump() for k, v in PRESETS.items()},
        "usage": month.model_dump(),
        "budget_unlimited": unlimited,
        "budget_blocked": settings.budget_blocks(month.cost_usd),
        "agents": settings.hybrid_table(AGENTS),
        "data_leaving": list(DATA_LEAVING),
    }


def add_cloud_routes(
    app: FastAPI,
    require_token: Callable[..., None],
    dispatcher: Callable[[], ModelDispatcher],
    web_dir: Path,
    audit: Callable[[], Any],
) -> None:
    def record_mode(new: CloudSettings, old: CloudSettings) -> None:
        if (new.mode, new.profile, new.model) != (old.mode, old.profile, old.model):
            audit().cloud_call(
                task_id="-",
                agent="owner",
                provider=new.key_id,
                model=new.model,
                prompt_tokens=0,
                completion_tokens=0,
                cost_usd=0.0,
                outcome=f"mode:{new.mode}",
                duration_ms=0.0,
            )

    def known(profile_id: str) -> str:
        import re

        if not re.match(PROFILE_ID, profile_id):
            raise HTTPException(status_code=404, detail="unknown profile")
        return profile_id

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
        unknown = set(body.agent_overrides) - set(AGENTS)
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
        if body.mode != "local" and not old.model.strip():
            raise HTTPException(status_code=422, detail="pick a provider profile and a model first")
        data = {
            **old.model_dump(),
            **body.model_dump(exclude={"accept_warning"}),
            "warning_accepted_at": accepted,
        }
        try:
            new = CloudSettings.model_validate(data)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()[0]["msg"]) from None
        d.settings.save(new)
        record_mode(new, old)
        return cloud_status(d)

    @app.post("/cloud/profiles", dependencies=[Depends(require_token)])
    def save_profile(body: ProfileChange) -> dict[str, Any]:
        """Add or change the profile for a preset (NVIDIA, Groq, ..., Custom)."""
        preset = PRESETS.get(body.preset)
        if preset is None:
            raise HTTPException(status_code=422, detail="unknown provider")
        d = dispatcher()
        old = d.settings.load()
        pid = body.preset  # one profile per provider preset (Custom is one more)
        base_url = (body.base_url or "").strip() or preset.base_url
        if not base_url:
            raise HTTPException(status_code=422, detail="type the provider's base URL")
        try:
            profile = Profile(
                label=body.label or preset.label,
                preset=body.preset,
                provider=preset.provider,
                base_url=base_url,
                model=body.model,
                price_input_per_mtok=body.price_input_per_mtok,
                price_output_per_mtok=body.price_output_per_mtok,
            )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()[0]["msg"]) from None
        new = old.with_profile(pid, profile, activate=body.activate)
        d.settings.save(new)
        record_mode(new, old)
        return cloud_status(d)

    @app.post("/cloud/profiles/{profile_id}/activate", dependencies=[Depends(require_token)])
    def activate(profile_id: str) -> dict[str, Any]:
        d = dispatcher()
        old = d.settings.load()
        try:
            new = old.activate(known(profile_id))
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown profile") from None
        d.settings.save(new)
        record_mode(new, old)
        return cloud_status(d)

    @app.delete("/cloud/profiles/{profile_id}", dependencies=[Depends(require_token)])
    def delete_profile(profile_id: str) -> dict[str, Any]:
        d = dispatcher()
        old = d.settings.load()
        if known(profile_id) not in old.profiles:
            raise HTTPException(status_code=404, detail="unknown profile")
        d.keys.delete(profile_id)
        new = old.without_profile(profile_id)
        d.settings.save(new)
        record_mode(new, old)
        return cloud_status(d)

    @app.post("/cloud/profiles/{profile_id}/key", dependencies=[Depends(require_token)])
    def save_key(profile_id: str, body: KeyChange) -> dict[str, Any]:
        d = dispatcher()
        if known(profile_id) not in d.settings.load().profiles:
            raise HTTPException(status_code=404, detail="save the provider first")
        try:
            stored = d.keys.set(profile_id, body.key)  # read back inside; raises if not
        except KeyStoreError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return {"saved": True, "profile": profile_id, "key_hint": key_hint(stored)}

    @app.delete("/cloud/profiles/{profile_id}/key", dependencies=[Depends(require_token)])
    def delete_key(profile_id: str) -> dict[str, Any]:
        dispatcher().keys.delete(known(profile_id))
        return {"saved": False, "profile": profile_id}

    @app.get("/cloud/profiles/{profile_id}/models", dependencies=[Depends(require_token)])
    async def models(profile_id: str) -> dict[str, Any]:
        try:
            ids = await run_in_threadpool(list_models, dispatcher(), known(profile_id))
        except CloudError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        return {"profile": profile_id, "models": ids}

    @app.post("/cloud/profiles/{profile_id}/test", dependencies=[Depends(require_token)])
    async def test_connection(profile_id: str) -> dict[str, Any]:
        d = dispatcher()
        try:
            settings = d.settings.load().activate(known(profile_id))
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown profile") from None
        return await run_in_threadpool(d.test_connection, settings)
