"""Rebuild the PC file index by hand and show what it covers:

    python -m memory.pc_index           # one full scan
    python -m memory.pc_index --watch   # scan, then keep it fresh until Ctrl+C

The backend (`python -m backend.api`) does the scan-and-watch itself while it runs.
"""

from __future__ import annotations

import argparse
import time

from memory.pc_index.index import FileIndex
from memory.pc_index.watcher import IndexService
from security.permissions.path_check import PathPolicy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--watch", action="store_true", help="keep watching after the scan")
    args = parser.parse_args()

    service = IndexService(FileIndex(), PathPolicy.from_config())
    start = time.perf_counter()
    report = service.start()
    print(f"index: {service.index.db_path}")
    print(f"folders indexed ({len(report.roots)}):")
    for root in report.roots:
        print(f"  {root}")
    print(
        f"{report.files} files, {report.changed} added or changed, {report.removed} removed "
        f"in {time.perf_counter() - start:.1f}s"
    )
    try:
        if args.watch:
            print("watching for changes; Ctrl+C to stop")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
