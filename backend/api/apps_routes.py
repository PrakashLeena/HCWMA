"""Routes for the Apps page and the first-run wizard (owner decision 2026-11-21).

All of them need the per-install token, like `/approvals`: only the owner's own window can
always-allow or revoke an app, switch an adapter, add a folder or finish setup. No agent tool
and no voice route reaches them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.apps_service import AppsService
from backend.reliability.service import ReliabilityService
from integrations.apps import setup_state


class AppChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["settings", "wizard"] = "settings"


class AdapterChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on: bool
    source: Literal["settings", "wizard", "offer"] = "settings"


class FolderChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=3, max_length=400)


class SetupFinish(BaseModel):
    model_config = ConfigDict(extra="forbid")

    how: Literal["finished", "skipped"]


def add_app_routes(
    app: FastAPI,
    require_token: Callable[..., None],
    apps: AppsService | None,
    reliability: ReliabilityService | None,
    web_dir: Any,
) -> None:
    def svc() -> AppsService:
        if apps is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "app inventory is off")
        return apps

    auth = [Depends(require_token)]

    @app.get("/apps", include_in_schema=False)
    def apps_page() -> FileResponse:
        return FileResponse(web_dir / "apps.html", media_type="text/html")

    @app.get("/setup", include_in_schema=False)
    def setup_page() -> FileResponse:
        return FileResponse(web_dir / "setup.html", media_type="text/html")

    @app.get("/apps/state", dependencies=auth)
    async def apps_state() -> dict[str, Any]:
        return await run_in_threadpool(svc().state)

    @app.post("/apps/scan", dependencies=auth)
    async def apps_scan() -> dict[str, Any]:
        return await run_in_threadpool(svc().scan, "owner")

    @app.post("/apps/{app_id}/allow", dependencies=auth)
    def apps_allow(app_id: str, body: AppChoice) -> dict[str, str]:
        try:
            name = svc().allow(app_id, body.source)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such app; scan again") from None
        except PermissionError as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from None
        return {"allowed": name}

    @app.post("/apps/{app_id}/revoke", dependencies=auth)
    def apps_revoke(app_id: str) -> dict[str, bool]:
        return {"revoked": svc().revoke_app(app_id)}

    @app.post("/adapters/{adapter_id}", dependencies=auth)
    def adapter_set(adapter_id: str, body: AdapterChoice) -> dict[str, Any]:
        try:
            svc().set_adapter(adapter_id, body.on, body.source)
        except ValueError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such adapter") from None
        return {"adapter": adapter_id, "on": body.on}

    @app.get("/setup/state", dependencies=auth)
    async def setup_info() -> dict[str, Any]:
        def build() -> dict[str, Any]:
            pc = reliability.health().model_dump() if reliability is not None else None
            return {
                "done": setup_state.setup_done(),
                "pc": pc,
                "folders": setup_state.folder_status(),
                "gmail": _gmail_status(),
            }

        return await run_in_threadpool(build)

    @app.post("/setup/folders", dependencies=auth)
    def setup_folder(body: FolderChoice) -> dict[str, str]:
        try:
            entry = setup_state.approve_folder(body.path)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
        return {"added": entry, "note": "takes effect when HCWMA restarts"}

    @app.post("/setup/finish", dependencies=auth)
    def setup_finish(body: SetupFinish) -> dict[str, bool]:
        setup_state.mark_setup_done(body.how)
        return {"done": True}


def _gmail_status() -> dict[str, Any]:
    try:
        from backend.reliability.health import gmail_probe

        ok, text = gmail_probe()
    except Exception as exc:  # noqa: BLE001 - shown as "could not check"
        return {"ok": None, "text": f"could not check ({type(exc).__name__})"}
    return {"ok": ok, "text": text}
