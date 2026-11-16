"""`get_system_time` — the harmless Step 6 test tool. T0, no side effects, no arguments.

Cross-platform standard library only, so it needs no OS-specific adapter.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict

from integrations.mcp.tool_schemas import PermissionTier, ToolManifest, ToolSpec


class GetSystemTimeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GetSystemTimeOutput(BaseModel):
    local: str  # ISO 8601 with offset
    utc: str  # ISO 8601, +00:00
    timezone: str
    utc_offset: str  # e.g. "+05:30"


def get_system_time(_: GetSystemTimeInput) -> GetSystemTimeOutput:
    now_utc = datetime.now(timezone.utc)
    local = now_utc.astimezone()
    offset = local.strftime("%z")  # "+0530"
    return GetSystemTimeOutput(
        local=local.isoformat(timespec="seconds"),
        utc=now_utc.isoformat(timespec="seconds"),
        timezone=local.tzname() or "unknown",
        utc_offset=f"{offset[:3]}:{offset[3:]}",
    )


MANIFEST = ToolManifest(
    name="get_system_time",
    version="0.1.0",
    created_by="developer",
    status="approved",
    route="pc",
    description="Return the PC's current local date and time, UTC time and time zone.",
    permission_tier=PermissionTier.T0,
    approval_required=False,
    side_effects="none",
    timeout_s=5,
)


def get_system_time_spec() -> ToolSpec:
    return ToolSpec(
        manifest=MANIFEST,
        input_model=GetSystemTimeInput,
        output_model=GetSystemTimeOutput,
        handler=get_system_time,
    )
