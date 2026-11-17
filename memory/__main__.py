"""The owner's view of HCWMA's memory (`docs/memory-policy.md` sections 7 and 8).

    python -m memory show [--scope profile]     list what HCWMA remembers, by scope
    python -m memory export <folder>            write all memory as JSON + Markdown
    python -m memory backup                     copy the database to the backup folder
    python -m memory backups                    list the backup files
    python -m memory restore <backup-file>      replace memory with a backup (current
                                                memory is backed up first)
    python -m memory forget <id> [<id> ...]     delete entries (and what came from them)
    python -m memory cleanup                    remove expired entries now (runs daily
                                                while HCWMA is running)
    python -m memory where                      show the data folder

These commands are the owner at the keyboard: they need no approval prompt.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from memory.config import resolve_data_dir
from memory.scopes import SOURCE_LABEL, STORED_SCOPES
from memory.service import MemoryService
from memory.storage.store import NotAMemoryDatabase
from security.audit.audit_logger import get_audit_logger


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m memory", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("show", help="list what HCWMA remembers")
    show.add_argument("--scope", choices=STORED_SCOPES)
    export = sub.add_parser("export", help="write all memory to JSON + Markdown")
    export.add_argument("folder", type=Path)
    sub.add_parser("backup", help="back up the memory database now")
    sub.add_parser("backups", help="list the backup files")
    restore = sub.add_parser("restore", help="replace memory with a backup file")
    restore.add_argument("file", type=Path)
    restore.add_argument("--yes", action="store_true", help="do not ask to confirm")
    forget = sub.add_parser("forget", help="delete entries by id")
    forget.add_argument("ids", nargs="+")
    sub.add_parser("cleanup", help="remove expired entries now")
    sub.add_parser("where", help="show the data folder")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "where":
        print(resolve_data_dir())
        return 0
    service = MemoryService.from_config(audit=get_audit_logger())

    if args.command == "show":
        scopes = [args.scope] if args.scope else list(STORED_SCOPES)
        entries = service.everything()
        for scope in scopes:
            rows = [e for e in entries if e.scope == scope]
            print(f"\n{scope} ({len(rows)})")
            for e in rows:
                extra = f" for {e.agent}" if e.agent else ""
                print(f"  {e.id}  {e.created_at[:10]}  [{SOURCE_LABEL[e.source]}{extra}]  {e.text}")
        flagged = service.stale_skill_notes()
        if flagged:
            print(f"\n{len(flagged)} skill note(s) unused for a long time; review or forget them.")
        return 0
    if args.command == "export":
        json_path, md_path = service.export(args.folder)
        print(f"wrote {json_path}\nwrote {md_path}")
        return 0
    if args.command == "backup":
        print(f"backup written: {service.backup()}")
        return 0
    if args.command == "backups":
        for path in service.backups():
            print(path)
        return 0
    if args.command == "restore":
        if not args.yes:
            answer = input(f"Replace all of HCWMA's memory with {args.file}? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                print("nothing changed")
                return 1
        try:
            count = service.restore(args.file)
        except NotAMemoryDatabase as exc:
            print(f"not restored: {exc}", file=sys.stderr)
            return 1
        print(f"restored {count} entries from {args.file}")
        return 0
    if args.command == "forget":
        result = service.forget(args.ids)
        missing = set(args.ids) - set(result.deleted_ids)
        print(
            f"deleted {len(result.deleted_ids)} entries, {result.also_deleted_derived} derived, "
            f"cleaned {result.backups_cleaned} backup(s)"
        )
        if missing:
            print(f"not found: {', '.join(sorted(missing))}")
        return 0
    if args.command == "cleanup":
        print(service.cleanup().model_dump())
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
