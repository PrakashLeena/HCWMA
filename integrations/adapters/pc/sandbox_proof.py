"""Step 13 prerequisite check: prove Windows Sandbox really isolates tool code.

    python -m integrations.adapters.pc.sandbox_proof

Writes a "hostile" probe tool into workspace/sandbox/proof_<time>/tool and runs it twice in
Windows Sandbox. Inside, the probe tries to read a host canary file, list the host drive, reach
the network, write to the read-only folders and leave a marker file. Then the host checks:

1. the canary (outside every mapped folder) cannot be read from inside;
2. there is no network (connect and DNS both fail);
3. the read-only folders are read-only;
4. the second run does not see the first run's marker, the VM is gone from `wsb list`, and
   nothing on the host changed outside the output folder.

Needs the portable Python in workspace/sandbox/python. The Qwen model is unloaded while the
VM runs and reloaded afterwards (if it was loaded). Takes about 1-2 minutes.
"""

from __future__ import annotations

import json
import secrets
import sys
from datetime import datetime

from integrations.adapters.pc.windows_sandbox import SANDBOX_HOME, WindowsSandbox

PROBE = r"""
import json, os, socket, unittest
from pathlib import Path

CANARY = Path(r"__CANARY__")
TOKEN = "__TOKEN__"
OUT = Path(r"C:\hcwma\out")
MARKER = Path(r"C:\Users\Public\hcwma_marker.txt")


def attempt(fn):
    try:
        return {"ok": True, "value": fn()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__}


def write(path):
    path.write_text("x")
    return "written"


class Probe(unittest.TestCase):
    def test_probe(self):
        found = {
            "read_canary": attempt(lambda: CANARY.read_text()),
            "canary_exists": attempt(CANARY.exists),
            "host_drive_exists": attempt(lambda: Path(CANARY.anchor).exists()),
            "guest_root": attempt(lambda: sorted(os.listdir(r"C:\hcwma"))),
            "tcp_connect": attempt(lambda: socket.create_connection(("1.1.1.1", 53), 5).close()),
            "dns": attempt(lambda: socket.getaddrinfo("example.com", 80)),
            "write_tool_dir": attempt(lambda: write(Path(r"C:\hcwma\tool\evil.txt"))),
            "write_python_dir": attempt(lambda: write(Path(r"C:\hcwma\python\evil.txt"))),
            "write_out_dir": attempt(lambda: write(OUT / "allowed.txt")),
            "marker_existed_before": MARKER.exists(),
        }
        found["leaked_token"] = TOKEN in json.dumps(found)
        MARKER.write_text("run-marker")
        (OUT / "probe.json").write_text(json.dumps(found))


if __name__ == "__main__":
    unittest.main()
"""


def _check(label: str, ok: bool, failures: list[str]) -> None:
    print(f"  [{'ok' if ok else 'FAIL'}] {label}")
    if not ok:
        failures.append(label)


def main() -> int:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = SANDBOX_HOME / f"proof_{stamp}"
    tool = base / "tool"
    tool.mkdir(parents=True)
    token = secrets.token_hex(16)
    canary = base / "canary_outside_mapped_folders.txt"
    canary.write_text(token, encoding="utf-8")
    (tool / "test_probe.py").write_text(
        PROBE.replace("__CANARY__", str(canary)).replace("__TOKEN__", token), encoding="utf-8"
    )
    before = sorted(p.name for p in base.iterdir())

    sandbox = WindowsSandbox()
    failures: list[str] = []
    probes = []
    for n in (1, 2):
        print(f"Run {n}: starting Windows Sandbox (model unloads first)...")
        res = sandbox.run_tool_tests(tool)
        print(f"  {res.seconds}s, events: {res.events}")
        probe_file = res.out_dir / "probe.json"
        probes.append(json.loads(probe_file.read_text()) if probe_file.is_file() else {})
        _check(f"run {n}: the tests ran and passed inside the sandbox", res.passed, failures)
        _check(f"run {n}: sandbox destroyed afterwards", res.destroyed, failures)
        _check(
            f"run {n}: sandbox no longer listed by wsb",
            res.sandbox_id is not None and res.sandbox_id not in sandbox.wsb.running(),
            failures,
        )

    p = probes[0]
    print("Isolation results (first run):")
    _check("cannot read the host canary file", not p["read_canary"]["ok"], failures)
    _check(
        "the canary token never appears in anything the code saw", not p["leaked_token"], failures
    )
    _check(
        "host drive does not exist inside",
        p["host_drive_exists"] == {"ok": True, "value": False} or not p["host_drive_exists"]["ok"],
        failures,
    )
    _check(
        "only python/tool/out are mapped",
        p["guest_root"].get("value") is not None
        and set(p["guest_root"]["value"]) <= {"python", "tool", "out"},
        failures,
    )
    _check("no network: TCP connect fails", not p["tcp_connect"]["ok"], failures)
    _check("no network: DNS fails", not p["dns"]["ok"], failures)
    _check("tool folder is read-only", not p["write_tool_dir"]["ok"], failures)
    _check("python folder is read-only", not p["write_python_dir"]["ok"], failures)
    _check("output folder is writable", p["write_out_dir"]["ok"], failures)
    print("Destroyed afterwards:")
    _check(
        "run 1 left a marker, run 2 did not see it (fresh VM each time)",
        probes[0]["marker_existed_before"] is False and probes[1]["marker_existed_before"] is False,
        failures,
    )
    after = sorted(p.name for p in base.iterdir())
    _check("host changed nothing outside the sandbox run folders", before == after, failures)
    _check("host canary unchanged", canary.read_text(encoding="utf-8") == token, failures)
    _check(
        "the tool's own folder on the host was not modified",
        not (tool / "evil.txt").exists(),
        failures,
    )

    print("PASS" if not failures else f"FAILED: {failures}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
