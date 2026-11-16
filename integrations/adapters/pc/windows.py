"""Windows-only PC adapter code, behind the shared interfaces in this package.

Recycle Bin: each drive keeps deleted files in `<drive>\\$Recycle.Bin\\<user SID>\\`. For
every deleted file there is a small `$I…` record (original path, size, deletion time) next
to the `$R…` file that holds the data. Only the `$I` records are read here — never the
deleted file's contents. Folders of other users are not readable by a normal account and
are skipped.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from integrations.adapters.pc.recycle_bin import RecycledItem

BIN_DIR_NAME = "$Recycle.Bin"
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
                    items.append(
                        RecycledItem(
                            item_id=f"{drive.drive or drive.name}|{data_name}",
                            original_path=path,
                            deleted_at=deleted,
                            size_bytes=size,
                        )
                    )
        return items
