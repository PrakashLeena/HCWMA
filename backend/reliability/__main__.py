"""Owner commands for reliability (build step 18). Run by hand, never by an agent:

    python -m backend.reliability backup                 full backup now (memory, planner,
                                                         tool state, settings, configs)
    python -m backend.reliability backups                list full backups (newest last)
    python -m backend.reliability restore <folder>       put a backup back (HCWMA must be
          [--yes] [--data-dir D] [--configs-dir C]       closed; current state backed up first)
    python -m backend.reliability health                 the health checks, in the terminal
    python -m backend.reliability tasks                  recent requests and how they ended
    python -m backend.reliability startup on|off|status  start HCWMA with Windows

`--data-dir` / `--configs-dir` restore somewhere else (for example an empty folder, to prove a
backup is good) instead of HCWMA's own data folder and the repository's configs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from backend.reliability.backup import BackupManager, BackupRefused, load_manifest
from backend.reliability.instance_lock import InstanceLock
from backend.reliability.journal import TaskJournal, journal_path
from memory.config import resolve_data_dir
from security.audit.audit_logger import get_audit_logger


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.reliability", description=__doc__.split("\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("backup", help="make a full backup now")
    sub.add_parser("backups", help="list full backups")
    restore = sub.add_parser("restore", help="restore a full backup")
    restore.add_argument("folder", type=Path)
    restore.add_argument("--yes", action="store_true", help="do not ask to confirm")
    restore.add_argument("--data-dir", type=Path, help="restore data into this folder instead")
    restore.add_argument("--configs-dir", type=Path, help="restore configs into this folder")
    sub.add_parser("health", help="show the health checks")
    sub.add_parser("tasks", help="recent requests and how they ended")
    startup = sub.add_parser("startup", help="start HCWMA with Windows")
    startup.add_argument("state", choices=["on", "off", "status"])
    return parser


def _restore(args: argparse.Namespace) -> int:
    data_dir = args.data_dir or resolve_data_dir()
    lock = InstanceLock(data_dir)
    if not lock.acquire("restore"):
        print(f"not restored: {lock.refusal()}", file=sys.stderr)
        return 1
    try:
        manager = BackupManager(data_dir=data_dir, configs_dir=args.configs_dir)
        try:
            manifest = manager.check(args.folder)
        except BackupRefused as exc:
            print(f"not restored: {exc}", file=sys.stderr)
            return 1
        print(f"backup {args.folder.name}: {len(manifest.items)} parts, made {manifest.created}")
        print(f"into: {data_dir}  and configs into: {manager.configs_dir}")
        if not args.yes:
            answer = input("Replace HCWMA's current data with this backup? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                print("nothing changed")
                return 1
        report = manager.restore(args.folder)
    finally:
        lock.release()
    get_audit_logger().reliability(
        action="restore", outcome="restored", reason=f"{args.folder.name} -> {data_dir}"
    )
    if report.safety_backup:
        print(f"the state before the restore was backed up to {report.safety_backup}")
    print(f"restored {len(report.restored)} parts:")
    for part in report.restored:
        print(f"  {part}")
    if report.memory_entries is not None:
        print(f"memory: {report.memory_entries} entries")
    if report.planner_items is not None:
        print(f"planner: {report.planner_items} items")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "backup":
        manager = BackupManager()
        folder = manager.backup(label="owner (command line)")
        get_audit_logger().reliability(action="backup", outcome="written", reason=folder.name)
        manifest = load_manifest(folder)
        print(f"backup written: {folder}")
        for item in manifest.items if manifest else []:
            print(f"  {item.path}  ({item.size} bytes)")
        print(f"kept: {len(manager.backups())} (newest {manager.keep})")
        return 0
    if args.command == "backups":
        for folder in BackupManager().backups():
            manifest = load_manifest(folder)
            count = len(manifest.items) if manifest else "?"
            label = f"  [{manifest.label}]" if manifest and manifest.label else ""
            print(f"{folder}  {count} parts{label}")
        return 0
    if args.command == "restore":
        return _restore(args)
    if args.command == "health":
        from backend.reliability.service import ReliabilityService

        report = ReliabilityService().health()
        for item in report.items:
            mark = {True: "OK", False: "!!"}.get(item.ok, "??")  # type: ignore[arg-type]
            print(f"{mark}  {item.name:<16} {item.value}")
        for warning in report.warnings:
            print(f"WARNING: {warning}")
        print(f"start with Windows: {report.start_with_windows}")
        print(f"settings: {report.settings}")
        return 0
    if args.command == "tasks":
        journal = TaskJournal(journal_path())
        for task in journal.recent(20):
            print(f"{task.started_at}  {task.status:<11} {task.label}  {task.reason}")
        return 0
    if args.command == "startup":
        from app.desktop.autostart import get_autostart

        autostart = get_autostart()
        if args.state == "on":
            print(f"on: {autostart.enable()}")
        elif args.state == "off":
            print("off (shortcut moved to the Recycle Bin)" if autostart.disable() else "was off")
        else:
            print("on" if autostart.is_enabled() else "off")
        get_audit_logger().reliability(action="startup", outcome=args.state)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
