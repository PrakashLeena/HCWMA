"""Notice that the PC slept (step 18).

The app's process survives sleep, but a model call or approval prompt that was in flight is
not something to silently carry on with hours later. A loop ticks every few seconds; when
the wall clock jumps far more than one tick, the PC was asleep (or the process was frozen),
and `on_wake` is called — the backend uses it to interrupt every running task and cancel
every approval prompt. Nothing is re-run.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

import anyio

log = logging.getLogger(__name__)

TICK_S = 5.0
SLEEP_GAP_S = 60.0


async def watch_for_sleep(
    on_wake: Callable[[float], Awaitable[None] | None],
    *,
    tick_s: float = TICK_S,
    gap_s: float = SLEEP_GAP_S,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
) -> None:
    """Runs until cancelled. `on_wake(seconds_asleep)` is called after each sleep."""
    last = clock()
    while True:
        await sleep(tick_s)
        now = clock()
        gap = now - last
        last = now
        if gap > gap_s:
            log.warning("the PC was asleep for about %.0f s", gap)
            try:
                result = on_wake(gap)
                if result is not None:
                    await result
            except Exception:  # noqa: BLE001 - the watch must keep running
                log.exception("handling the wake-up failed")
