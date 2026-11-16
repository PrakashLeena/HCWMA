"""Shared Recycle Bin interface for the PC tools. OS-specific code lives in `<os>.py`.

Step 8 only lists what is in the bin (read-only). Restoring arrives with the approval
workflow in Step 9.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class RecycledItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: str  # opaque handle a later restore tool can use
    original_path: str
    deleted_at: datetime
    size_bytes: int


class RecycleBinUnavailable(RuntimeError):
    """The Recycle Bin cannot be read on this OS (yet)."""


class RecycleBin(Protocol):
    def list_items(self, drives: Sequence[Path]) -> list[RecycledItem]:
        """Items deleted from the given drives by the current user."""
        ...


class UnsupportedRecycleBin:
    def list_items(self, drives: Sequence[Path]) -> list[RecycledItem]:
        raise RecycleBinUnavailable(f"reading the Recycle Bin is not supported on {sys.platform}")


def get_recycle_bin() -> RecycleBin:
    if sys.platform == "win32":
        from integrations.adapters.pc.windows import WindowsRecycleBin

        return WindowsRecycleBin()
    return UnsupportedRecycleBin()
