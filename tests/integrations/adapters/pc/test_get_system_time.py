"""Step 6: `get_system_time`, the harmless test tool (add-tool skill test groups).

Not applicable to this tool: protected paths (no path argument), credentials (no secrets),
approval-required blocking (T0, no side effects). Those groups are covered generically in
tests/agents/runtime/test_policy.py and tests/security/.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from integrations.adapters.pc.system_time import (
    GetSystemTimeInput,
    get_system_time,
    get_system_time_spec,
)


def test_success_returns_consistent_times() -> None:
    out = get_system_time(GetSystemTimeInput())
    local = datetime.fromisoformat(out.local)
    utc = datetime.fromisoformat(out.utc)
    assert local.tzinfo is not None
    assert abs((local - utc).total_seconds()) < 5
    assert out.utc_offset.startswith(("+", "-"))
    assert out.timezone


def test_unknown_arguments_rejected() -> None:
    with pytest.raises(ValidationError):
        GetSystemTimeInput.model_validate({"path": "C:/Windows"})


def test_manifest_is_harmless() -> None:
    m = get_system_time_spec().manifest
    assert m.permission_tier == "T0"
    assert m.side_effects == "none"
    assert not m.approval_required
    assert m.permissions.network == [] and m.permissions.filesystem == []
    assert m.permissions.secrets == []
