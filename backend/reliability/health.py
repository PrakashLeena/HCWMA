"""The health page's checks (build step 18). Each check is plain code, read-only, run with a
time limit, and reports `ok` True / False / None (None = could not tell).

Gmail: whether the saved login still gets an access token from Google (no mail is read).
WhatsApp: whether WhatsApp Desktop's chat list is showing (no chat or message is read).
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from pydantic import BaseModel

Probe = Callable[[], tuple[bool | None, str]]


class HealthItem(BaseModel):
    name: str
    ok: bool | None
    value: str


class HealthReport(BaseModel):
    items: list[HealthItem]
    warnings: list[str]
    heavy_tasks_refused: str | None
    model_paused: bool
    settings: dict[str, Any]
    start_with_windows: bool | None
    recovery: str
    checked_at: str


def gmail_probe() -> tuple[bool | None, str]:
    from integrations.adapters.official_api.gmail_auth import (
        KEY_REFRESH,
        GmailLoginNeeded,
        KeyringStore,
        TokenProvider,
        account_address,
    )

    store = KeyringStore()
    if not store.get(KEY_REFRESH):
        return (
            False,
            "not logged in (run: python -m integrations.adapters.official_api.gmail_login login)",
        )
    try:
        TokenProvider(store).access_token()
    except GmailLoginNeeded as exc:
        return False, str(exc)
    except RuntimeError as exc:
        return None, f"could not check: {exc}"
    return True, f"valid ({account_address(store) or 'account on file'})"


def whatsapp_probe() -> tuple[bool | None, str]:
    if sys.platform != "win32":
        return None, "only checked on Windows"
    from integrations.adapters.pc.windows_whatsapp import WindowsWhatsApp

    linked, text = WindowsWhatsApp().link_state()
    return linked, text


class CachedProbe:
    """Run a slow check at most once per `ttl_s`; the health page may be opened often."""

    def __init__(self, probe: Probe, ttl_s: float, clock: Callable[[], float] = time.monotonic):
        self._probe = probe
        self._ttl = ttl_s
        self._clock = clock
        self._value: tuple[bool | None, str] | None = None
        self._at = 0.0

    def __call__(self) -> tuple[bool | None, str]:
        now = self._clock()
        if self._value is None or now - self._at >= self._ttl:
            self._value = self._probe()
            self._at = now
        return self._value


def run_probes(probes: dict[str, Probe], timeout_s: float = 8.0) -> dict[str, HealthItem]:
    """Run every probe at once; a slow or failing probe reports itself, never blocks others."""
    results: dict[str, HealthItem] = {}
    pool = ThreadPoolExecutor(max_workers=max(1, len(probes)), thread_name_prefix="hcwma-health")
    try:
        futures = {name: pool.submit(probe) for name, probe in probes.items()}
        deadline = time.monotonic() + timeout_s
        for name, future in futures.items():
            try:
                ok, value = future.result(timeout=max(0.0, deadline - time.monotonic()))
            except FutureTimeout:
                ok, value = None, "no answer in time"
            except Exception as exc:  # noqa: BLE001 - shown as "could not check"
                ok, value = None, f"could not check ({type(exc).__name__})"
            results[name] = HealthItem(name=name, ok=ok, value=value)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results
