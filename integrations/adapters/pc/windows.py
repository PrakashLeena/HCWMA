"""Windows-only PC adapter code, behind the shared interfaces in this package.

Recycle Bin: each drive keeps deleted files in `<drive>\\$Recycle.Bin\\<user SID>\\`. For
every deleted file there is a small `$I…` record (original path, size, deletion time) next
to the `$R…` file that holds the data. Only the `$I` records are read here — never the
deleted file's contents. Folders of other users are not readable by a normal account and
are skipped.
"""

from __future__ import annotations

import os
import re
import struct
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from integrations.adapters.pc.recycle_bin import DestinationCheck, RecycleBinError, RecycledItem

BIN_DIR_NAME = "$Recycle.Bin"
# "<drive>|$R<name>" as produced by `list_items`; the name may not contain a path separator.
_ITEM_ID = re.compile(r'^(?P<drive>[^|\\/]{1,60})\|(?P<name>\$R[^\\/|:*?"<>]{1,200})$')
_FILETIME_EPOCH_OFFSET = 116_444_736_000_000_000  # 1601-01-01 -> 1970-01-01, in 100 ns
_HEADER = struct.Struct("<qqq")  # version, original size, deletion FILETIME
_MAX_RECORD_BYTES = 70_000  # a record holds one path; anything larger is not a record


def parse_info_record(data: bytes) -> tuple[str, int, datetime] | None:
    """(original path, size, deletion time) from a `$I` record, or None if malformed."""
    if len(data) < _HEADER.size + 4:
        return None
    version, size, filetime = _HEADER.unpack_from(data, 0)
    if version == 1:
        raw = data[_HEADER.size : _HEADER.size + 520]
    elif version == 2:
        (chars,) = struct.unpack_from("<i", data, _HEADER.size)
        if chars <= 0:
            return None
        raw = data[_HEADER.size + 4 : _HEADER.size + 4 + chars * 2]
    else:
        return None
    path = raw.decode("utf-16-le", errors="replace").split("\x00", 1)[0]
    if not path or size < 0:
        return None
    try:
        deleted = datetime.fromtimestamp(
            (filetime - _FILETIME_EPOCH_OFFSET) / 10_000_000, tz=timezone.utc
        )
    except (OverflowError, OSError, ValueError):
        return None
    return path, size, deleted


class WindowsRecycleBin:
    def list_items(self, drives: Sequence[Path]) -> list[RecycledItem]:
        items: list[RecycledItem] = []
        for drive in drives:
            try:
                user_dirs = [d for d in (drive / BIN_DIR_NAME).iterdir() if d.is_dir()]
            except OSError:
                continue  # no bin on this drive
            for user_dir in user_dirs:
                try:
                    records = [f for f in user_dir.iterdir() if f.name.upper().startswith("$I")]
                except OSError:
                    continue  # another user's bin
                for record in records:
                    try:
                        if record.stat().st_size > _MAX_RECORD_BYTES:
                            continue
                        parsed = parse_info_record(record.read_bytes())
                    except OSError:
                        continue
                    if parsed is None:
                        continue
                    path, size, deleted = parsed
                    data_name = "$R" + record.name[2:]
                    if not (user_dir / data_name).exists():
                        continue  # a leftover record: the file itself is no longer in the bin
                    items.append(
                        RecycledItem(
                            item_id=f"{_drive_label(drive)}|{data_name}",
                            original_path=path,
                            deleted_at=deleted,
                            size_bytes=size,
                        )
                    )
        return items

    def _locate(self, item_id: str, drives: Sequence[Path]) -> tuple[Path, Path, RecycledItem]:
        """(`$R` data file, `$I` record, item) for an id produced by `list_items`."""
        match = _ITEM_ID.match(item_id)
        if match is None:
            raise RecycleBinError("that is not a Recycle Bin item id")
        label, data_name = match["drive"], match["name"]
        for drive in drives:
            if _drive_label(drive).lower() != label.lower():
                continue
            try:
                user_dirs = [d for d in (drive / BIN_DIR_NAME).iterdir() if d.is_dir()]
            except OSError:
                continue
            for user_dir in user_dirs:
                data = user_dir / data_name
                record = user_dir / ("$I" + data_name[2:])
                try:
                    if not data.is_file() or record.stat().st_size > _MAX_RECORD_BYTES:
                        continue
                    parsed = parse_info_record(record.read_bytes())
                except OSError:
                    continue
                if parsed is None:
                    continue
                path, size, deleted = parsed
                item = RecycledItem(
                    item_id=item_id, original_path=path, deleted_at=deleted, size_bytes=size
                )
                return data, record, item
        raise RecycleBinError("that item is not in the Recycle Bin (any more), or it is a folder")

    def find(self, item_id: str, drives: Sequence[Path]) -> RecycledItem | None:
        try:
            return self._locate(item_id, drives)[2]
        except RecycleBinError:
            return None

    def restore(self, item_id: str, drives: Sequence[Path], allow: DestinationCheck) -> Path:
        data, record, item = self._locate(item_id, drives)
        destination = allow(item.original_path)  # raises PathDenied outside policy
        if destination.exists():
            raise RecycleBinError(f"a file already exists at '{destination}'; nothing was changed")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Same drive as the bin, so this is a rename. It fails rather than overwrite.
            os.rename(data, destination)
        except OSError as exc:
            raise RecycleBinError(f"could not restore: {type(exc).__name__}") from exc
        try:
            # The bin's own ~100-byte bookkeeping record for a file that is no longer deleted;
            # Windows removes it too on Restore, and leaving it shows a broken entry in the
            # Recycle Bin. Owner decision 2026-11-17: this is the one reviewed exception to
            # "no permanent delete" (rule 9). It is never an owner file: `_locate` only
            # returns a `$I…` record inside `$Recycle.Bin`.
            record.unlink()
        except OSError:
            pass
        return destination


def _drive_label(drive: Path) -> str:
    return drive.drive or drive.name
