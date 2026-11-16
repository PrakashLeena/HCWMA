"""Step 8: reading Windows Recycle Bin records, against a fake bin in a temp folder.

The parser is plain bytes handling, so these tests run on any OS.
"""

from __future__ import annotations

import struct
from datetime import datetime, timezone
from pathlib import Path

from integrations.adapters.pc.recycle_bin import UnsupportedRecycleBin, get_recycle_bin
from integrations.adapters.pc.windows import (
    BIN_DIR_NAME,
    WindowsRecycleBin,
    parse_info_record,
)

DELETED = datetime(2026, 11, 10, 9, 30, tzinfo=timezone.utc)


def _filetime(when: datetime) -> int:
    return int(when.timestamp() * 10_000_000) + 116_444_736_000_000_000


def record_v2(path: str, size: int = 1234, when: datetime = DELETED) -> bytes:
    encoded = (path + "\x00").encode("utf-16-le")
    return struct.pack("<qqqi", 2, size, _filetime(when), len(path) + 1) + encoded


def record_v1(path: str, size: int = 99, when: datetime = DELETED) -> bytes:
    encoded = path.encode("utf-16-le").ljust(520, b"\x00")
    return struct.pack("<qqq", 1, size, _filetime(when)) + encoded


def test_parse_windows_10_record() -> None:
    parsed = parse_info_record(record_v2(r"C:\Users\me\Documents\thésis final.docx"))
    assert parsed == (r"C:\Users\me\Documents\thésis final.docx", 1234, DELETED)


def test_parse_older_fixed_width_record() -> None:
    assert parse_info_record(record_v1(r"D:\work\plan.txt")) == (r"D:\work\plan.txt", 99, DELETED)


def test_malformed_records_are_ignored() -> None:
    assert parse_info_record(b"") is None
    assert parse_info_record(b"\x00" * 10) is None
    assert parse_info_record(struct.pack("<qqqi", 7, 1, 1, 3) + b"abcdef") is None  # version
    assert parse_info_record(struct.pack("<qqqi", 2, 1, _filetime(DELETED), 0)) is None
    assert parse_info_record(struct.pack("<qqqi", 2, -5, _filetime(DELETED), 2) + b"a\x00") is None
    assert parse_info_record(struct.pack("<qqqi", 2, 1, 2**62, 2) + b"a\x00\x00\x00") is None


def test_list_items_reads_only_info_records(tmp_path: Path) -> None:
    user = tmp_path / BIN_DIR_NAME / "S-1-5-21-1111-2222-3333-1001"
    user.mkdir(parents=True)
    (user / "$IAB12CD.docx").write_bytes(record_v2(r"C:\docs\thesis.docx", size=500))
    (user / "$RAB12CD.docx").write_bytes(b"the deleted file's own data, never read")
    (user / "$IZZ99ZZ.txt").write_bytes(b"garbage")
    (user / "$IHUGE00.bin").write_bytes(b"\x00" * 80_000)
    (user / "desktop.ini").write_text("[.ShellClassInfo]", encoding="utf-8")
    (tmp_path / BIN_DIR_NAME / "not-a-folder.txt").write_text("x", encoding="utf-8")

    items = WindowsRecycleBin().list_items([tmp_path, tmp_path / "no-such-drive"])
    assert len(items) == 1
    item = items[0]
    assert item.original_path == r"C:\docs\thesis.docx"
    assert item.size_bytes == 500 and item.deleted_at == DELETED
    assert item.item_id.endswith("|$RAB12CD.docx")


def test_other_platforms_say_unsupported(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("integrations.adapters.pc.recycle_bin.sys.platform", "linux")
    assert isinstance(get_recycle_bin(), UnsupportedRecycleBin)
