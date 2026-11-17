"""Shared Recycle Bin interface for the PC tools. OS-specific code lives in `<os>.py`.

Step 8 lists what is in the bin (read-only). Step 9 adds restoring one item to where it
was deleted from, and sending a file to the bin (`send2trash`, the only way HCWMA deletes).
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class RecycledItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: str  # opaque handle the restore tool uses
    original_path: str
    deleted_at: datetime
    size_bytes: int


class RecycleBinUnavailable(RuntimeError):
    """The Recycle Bin cannot be used on this OS (yet)."""


class RecycleBinError(RuntimeError):
    """A restore could not be done (no such item, or something is in the way)."""


# Given the item's original path, returns the checked destination or raises `PathDenied`.
DestinationCheck = Callable[[str], Path]


class RecycleBin(Protocol):
    def list_items(self, drives: Sequence[Path]) -> list[RecycledItem]:
        """Items deleted from the given drives by the current user."""
        ...

    def find(self, item_id: str, drives: Sequence[Path]) -> RecycledItem | None:
        """The item with this id, if it is still in the bin on one of the drives."""
        ...

    def restore(self, item_id: str, drives: Sequence[Path], allow: DestinationCheck) -> Path:
        """Put the item back where it was deleted from. Never overwrites an existing file."""
        ...


class UnsupportedRecycleBin:
    def _no(self) -> RecycleBinUnavailable:
        return RecycleBinUnavailable(f"the Recycle Bin is not supported on {sys.platform} yet")

    def list_items(self, drives: Sequence[Path]) -> list[RecycledItem]:
        raise self._no()

    def find(self, item_id: str, drives: Sequence[Path]) -> RecycledItem | None:
        raise self._no()

    def restore(self, item_id: str, drives: Sequence[Path], allow: DestinationCheck) -> Path:
        raise self._no()


def get_recycle_bin() -> RecycleBin:
    if sys.platform == "win32":
        from integrations.adapters.pc.windows import WindowsRecycleBin

        return WindowsRecycleBin()
    return UnsupportedRecycleBin()


def move_to_recycle_bin(path: Path) -> None:
    """The one way HCWMA deletes (architecture rule 9). Works on Windows, macOS and Linux."""
    from send2trash import send2trash

    send2trash(str(path))
