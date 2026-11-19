"""Step 18 live check: HCWMA survives a forced restart in the middle of a task.

    python -m backend.reliability.crash_drill      (needs Ollama; ~3-6 min on CPU)

Close the HCWMA app first (it uses the same port). The drill uses its own data folder under
`workspace/step18_drill_<time>/` (HCWMA_DATA_DIR), so your real memory, planner and journal
are not touched:

1. starts the real backend (`python -m backend.api`) as a separate process,
2. asks it to delete a bait file in that folder — the File agent asks for approval,
3. while the approval prompt is waiting, kills the backend process tree (like a crash,
   a forced restart or pulled power: no clean-up code runs),
4. starts the backend again and checks: the request is reported interrupted, the approval
   prompt is gone, the journal and audit log say so, nothing is re-run (no new task, the bait
   file is unchanged) and new requests are accepted again,
5. stops the backend.

This is an owner check script: the processes it starts are HCWMA's own backend.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil

from backend.auth.token import get_or_create_token
from backend.config import load_backend_config
from backend.reliability.journal import TaskJournal, journal_path
from memory.config import REPO_ROOT
from security.audit.audit_logger import get_audit_logger

BAIT_TEXT = "Old draft, kept on purpose. If this file disappears, the drill failed.\n"


class Api:
    def __init__(self, token: str) -> None:
        config = load_backend_config()
        self.base = f"http://{config.host}:{config.port}"
        self._token = token
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method: str, path: str, body: Any = None, timeout: float = 10) -> Any:
        data = (
            json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
        )
        req = urllib.request.Request(self.base + path, data=data, method=method)  # noqa: S310
        req.add_header("Authorization", f"Bearer {self._token}")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        with self._opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    def up(self) -> bool:
        try:
            req = urllib.request.Request(self.base + "/health")  # noqa: S310
            with self._opener.open(req, timeout=2) as resp:
                return json.loads(resp.read().decode()).get("status") == "ok"
        except (OSError, ValueError):
            return False


_STARTED: list[subprocess.Popen[bytes]] = []


def start_backend(env: dict[str, str], log: Path) -> subprocess.Popen[bytes]:
    out = log.open("ab")
    proc = subprocess.Popen(  # noqa: S603 - this repository's own backend, fixed arguments
        [sys.executable, "-m", "backend.api"], cwd=REPO_ROOT, env=env, stdout=out, stderr=out
    )
    _STARTED.append(proc)
    return proc


def kill_tree(proc: subprocess.Popen[bytes]) -> int:
    """Kill the backend and every child (the MCP tool server): a hard crash, no clean-up."""
    try:
        parent = psutil.Process(proc.pid)
    except psutil.NoSuchProcess:
        return 0
    victims = [*parent.children(recursive=True), parent]
    for p in victims:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(victims, timeout=15)
    return len(victims)


def wait_until(check: Any, timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(1)
    raise SystemExit(f"FAIL: {what} did not happen within {timeout_s:.0f} s")


def main() -> int:
    """Run the drill; whatever happens, no backend it started is left running."""
    try:
        return _run()
    finally:
        for proc in _STARTED:
            if proc.poll() is None:
                kill_tree(proc)


def _run() -> int:
    token = get_or_create_token()
    api = Api(token)
    if api.up():
        print("Close the HCWMA app (tray: Quit HCWMA) first: the drill needs its port.")
        return 1
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = REPO_ROOT / "workspace" / f"step18_drill_{stamp}"
    data = root / "data"
    data.mkdir(parents=True)
    bait = root / "old draft.txt"
    bait.write_text(BAIT_TEXT, encoding="utf-8")
    env = {**os.environ, "HCWMA_DATA_DIR": str(data)}
    log = root / "backend.log"
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}{'  - ' + detail if detail else ''}")

    print(f"drill folder: {root}")
    print("1. starting the backend ...")
    proc = start_backend(env, log)
    wait_until(api.up, 120, "the backend start")

    print("2. asking it to delete the bait file (the File agent will ask for approval) ...")
    request = f'Delete the file "{bait}"'
    reply: list[Any] = []

    def send() -> None:
        try:
            reply.append(
                api.call(
                    "POST",
                    "/chat",
                    {"messages": [{"role": "user", "content": request}]},
                    timeout=900,
                )
            )
        except urllib.error.HTTPError as exc:  # e.g. refused: low RAM right now
            reply.append(f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}")
        except Exception as exc:  # noqa: BLE001 - the backend is killed under it
            reply.append(type(exc).__name__)

    for attempt in range(1, 4):
        reply.clear()
        threading.Thread(target=send, daemon=True).start()
        wait_until(
            lambda: reply or api.call("GET", "/approvals")["pending"], 600, "the approval prompt"
        )
        if not reply:
            break
        # The request ended without asking (refused, e.g. RAM briefly under 1 GB, or the
        # model answered instead). Say why, wait, and ask again.
        print(f"   attempt {attempt}: no approval prompt; the reply was: {reply[0]}")
        if attempt == 3:
            raise SystemExit("FAIL: no approval prompt after 3 attempts")
        time.sleep(30)
    [prompt] = api.call("GET", "/approvals")["pending"]
    print(f"   approval waiting: {prompt['tool']} ({prompt['summary'][:80]})")
    running = TaskJournal(journal_path(data)).running()
    check(
        "the request is on the journal as running",
        len(running) == 1,
        running[0].label if running else "",
    )

    print("3. killing the backend process tree mid-task (forced restart) ...")
    killed = kill_tree(proc)
    wait_until(lambda: not api.up(), 30, "the backend going down")
    time.sleep(1)
    check(f"backend killed ({killed} processes)", not api.up())
    check("the bait file is still there after the crash", bait.read_text("utf-8") == BAIT_TEXT)

    print("4. starting the backend again ...")
    proc = start_backend(env, log)
    wait_until(api.up, 120, "the restart")
    recovery = api.call("GET", "/recovery")
    check(
        "the interrupted request is reported once, not re-run",
        len(recovery["tasks"]) == 1 and recovery["tasks"][0].startswith("Delete the file"),
        recovery["message"][:160],
    )
    check("the old approval prompt is gone", api.call("GET", "/approvals")["pending"] == [])
    journal = TaskJournal(journal_path(data))
    tasks = journal.recent()
    check(
        "journal: the task is interrupted",
        [t.status for t in tasks] == ["interrupted"],
        tasks[0].reason if tasks else "",
    )
    check("journal: the approval is cancelled", journal.pending_approvals() == [])
    print("   waiting 20 s to be sure nothing starts by itself ...")
    time.sleep(20)
    check("nothing was re-run (no new task)", len(TaskJournal(journal_path(data)).recent()) == 1)
    check("the bait file is unchanged", bait.read_text("utf-8") == BAIT_TEXT)
    records = get_audit_logger().read_all()
    check(
        "audit: recovery record written",
        any(r.event == "recovery" and r.task_id == tasks[0].id for r in records),
    )
    # The prompt belongs to the File agent's sub-task, so it is found by its own id.
    tag = f"approval {prompt['id'][:8]}"
    prompt_records = [r for r in records if r.event == "approval" and r.reason.startswith(tag)]
    check(
        "audit: approval cancelled at start",
        any(r.outcome == "cancelled" and "at start" in r.reason for r in prompt_records),
    )
    check(
        "audit: the delete never ran",
        not any(
            r.event == "tool_call" and r.tool == "delete_file" and root.name in str(r.args)
            for r in records
        ),
    )
    health = api.call("GET", "/health/details", timeout=30)
    check("the health page shows the recovery", "interrupted" in health["recovery"])

    print("5. stopping the backend ...")
    api.call("POST", "/stop")
    kill_tree(proc)
    failed = [name for name, ok, _ in results if not ok]
    print(
        f"\n{len(results) - len(failed)}/{len(results)} PASS"
        + (f"; failed: {failed}" if failed else "")
    )
    print(f"(drill folder kept for inspection: {root})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
