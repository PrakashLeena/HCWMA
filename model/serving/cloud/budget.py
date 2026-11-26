"""Cloud usage this month: calls, tokens and cost, in `<data dir>/cloud_usage.json`.

Numbers only, never prompt text. A new calendar month starts from zero (older months are
kept for the cost history, newest 12).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from memory.config import resolve_data_dir

USAGE_FILE = "cloud_usage.json"
KEEP_MONTHS = 12


class MonthUsage(BaseModel):
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    fallbacks: int = 0


def cost_of(prompt_tokens: int, completion_tokens: int, price_in: float, price_out: float) -> float:
    return (prompt_tokens * price_in + completion_tokens * price_out) / 1_000_000


class UsageLedger:
    def __init__(
        self, data_dir: Path | None = None, clock: Callable[[], datetime] = datetime.now
    ) -> None:
        self._data_dir = data_dir
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return (self._data_dir or resolve_data_dir()) / USAGE_FILE

    def _month(self) -> str:
        return self._clock().strftime("%Y-%m")

    def _load_all(self) -> dict[str, MonthUsage]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return {k: MonthUsage.model_validate(v) for k, v in raw.items()}
        except (OSError, ValueError, ValidationError, AttributeError):
            return {}

    def this_month(self) -> MonthUsage:
        with self._lock:
            return self._load_all().get(self._month(), MonthUsage())

    def history(self) -> dict[str, MonthUsage]:
        with self._lock:
            return self._load_all()

    def add(
        self,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float = 0.0,
        fallback: bool = False,
    ) -> MonthUsage:
        with self._lock:
            months = self._load_all()
            key = self._month()
            usage = months.get(key, MonthUsage())
            if fallback:
                usage.fallbacks += 1
            else:
                usage.calls += 1
                usage.prompt_tokens += prompt_tokens
                usage.completion_tokens += completion_tokens
                usage.cost_usd = round(usage.cost_usd + cost_usd, 6)
            months[key] = usage
            kept = dict(sorted(months.items())[-KEEP_MONTHS:])
            path = self.path
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({k: v.model_dump() for k, v in kept.items()}, f, indent=1)
            Path(tmp).replace(path)
            return usage
