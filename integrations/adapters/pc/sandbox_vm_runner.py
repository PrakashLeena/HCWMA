"""Runs INSIDE Windows Sandbox (build step 13). Not imported by HCWMA.

Started by `windows_sandbox.py` as:  python -I -B sandbox_vm_runner.py <tool_dir> <out_dir>

Standard library only (it runs on the portable Python). It discovers and runs the tool's
`test*.py` unit tests and writes `results.json` to <out_dir>, because `wsb exec` does not
return program output. The host treats results.json as untrusted data.
"""

from __future__ import annotations

import io
import json
import sys
import time
import unittest
from pathlib import Path

MAX_TEXT = 20_000


def main(argv: list[str]) -> int:
    tool_dir, out_dir = Path(argv[1]), Path(argv[2])
    started = time.time()
    (out_dir / "started.json").write_text(json.dumps({"python": sys.version}), encoding="utf-8")
    sys.path.insert(0, str(tool_dir))
    stream = io.StringIO()
    suite = unittest.TestLoader().discover(
        str(tool_dir), pattern="test*.py", top_level_dir=str(tool_dir)
    )
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    payload = {
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "passed": result.wasSuccessful() and result.testsRun > 0,
        "seconds": round(time.time() - started, 2),
        "python": sys.version.split()[0],
        "output": stream.getvalue()[-MAX_TEXT:],
    }
    tmp = out_dir / "results.json.tmp"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(out_dir / "results.json")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
