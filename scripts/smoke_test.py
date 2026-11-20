"""HCWMA v1.0 smoke test: one end-to-end request per agent, through the running desktop app.

    python -m app.desktop          # first, in another terminal (or the start shortcut)
    python scripts/smoke_test.py   # ~10-20 min on CPU; --only planner,web_research to pick cases

Safe actions only: nothing is sent, deleted, moved or changed. While it runs, any approval
prompt that appears is DENIED at once and the case fails (a safe request must never need one).
At the end the audit log is checked: no T3/T4 tool (send, delete, move, settings change,
click/type) may have run during the test.
Test files live in workspace/smoke_test_<time>/ (gitignored) and are left there.
"""

# ruff: noqa: E501  (the case table reads best one case per line)

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agents.registry.agent_registry import AgentRegistry  # noqa: E402
from backend.auth.token import get_or_create_token  # noqa: E402
from backend.config import load_backend_config  # noqa: E402
from security.audit.audit_logger import resolve_audit_path  # noqa: E402

CASE_TIMEOUT_S = 420
TOOL_REQUIRED = {c.id for c in AgentRegistry.from_dir().workers() if c.requires_tool}


class Backend:
    def __init__(self) -> None:
        cfg = load_backend_config()
        self.base = f"http://{cfg.host}:{cfg.port}"  # host is always 127.0.0.1 (BackendConfig)
        self.token = get_or_create_token()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method: str, path: str, body: Any = None, timeout: float = 30) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)  # noqa: S310 - 127.0.0.1
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        with self.opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"null")

    def up(self) -> bool:
        try:
            return self.call("GET", "/health", timeout=3)["status"] == "ok"
        except (urllib.error.URLError, OSError, ValueError):
            return False


@dataclass
class Case:
    name: str
    agent: str  # expected first agent; "" = the Router answers itself
    message: str
    expect_any: list[str] = field(default_factory=list)  # one must appear in the reply
    needs: str = ""  # a Health item that must be OK, else SKIP


@dataclass
class Result:
    case: str
    agent: str
    outcome: str  # PASS / FAIL / SKIP
    seconds: float
    note: str
    reply: str = ""


class ApprovalGuard:
    """Denies every approval prompt that appears while the smoke test runs."""

    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self.denied: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> ApprovalGuard:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(5)

    def _run(self) -> None:
        while not self._stop.wait(0.5):
            try:
                for p in self.backend.call("GET", "/approvals")["pending"]:
                    self.backend.call("POST", f"/approvals/{p['id']}", {"decision": "deny"})
                    self.denied.append(f"{p['agent']}:{p['tool']}")
            except (urllib.error.URLError, OSError, ValueError, KeyError):
                pass


def make_files(folder: Path) -> dict[str, str]:
    folder.mkdir(parents=True, exist_ok=True)
    marker = f"smoke-marker-{folder.name[-6:]}.txt"
    (folder / marker).write_text("HCWMA smoke test marker file.\n", encoding="utf-8")
    (folder / "notes.txt").write_text(
        "Biology notes. Photosynthesis turns light, water and carbon dioxide into glucose and "
        "oxygen. It happens in the chloroplasts of plant cells.\n",
        encoding="utf-8",
    )
    from PIL import Image

    Image.new("RGB", (640, 480), (30, 120, 200)).save(folder / "photo.png")
    # The full path, as the owner would paste it: agents resolve relative paths differently.
    return {"marker": marker, "folder": str(folder)}


def cases(f: dict[str, str]) -> list[Case]:
    d = f["folder"]
    return [
        Case("router", "", "Hello! In one sentence, what can you help me with?", []),
        Case("echo", "echo", "What time is it on this PC?", [":"]),
        Case("file_system", "file_system", f"Where is the file called {f['marker']}?", [f["marker"]]),
        Case("documents_study", "documents_study", f"Open {d}/notes.txt and summarize what it says", ["photosynthesis", "Photosynthesis"]),
        Case("coding", "coding", "Explain what this Python code does: print(sum(range(5)))", ["10", "sum"]),
        Case("debugging", "debugging", "This Python code crashes with ZeroDivisionError: print(1/0). What is wrong?", ["zero", "Zero"]),
        Case("image_editing", "image_editing", f"What size is the picture {d}/photo.png?", ["640"]),
        Case("web_research", "web_research", "Read https://example.com and tell me what the page says", ["Example Domain", "example"]),
        Case("memory_librarian", "memory_librarian", "What do you remember about my preferences?", []),
        Case("planner", "planner", "What reminders and deadlines do I have?", []),
        Case("gmail_read", "communication", "Read my latest email", [], needs="Gmail login"),
        Case("whatsapp_list", "communication", "List my WhatsApp chats", [], needs="WhatsApp linked"),
        Case("system_settings", "system_settings", "Is Bluetooth on or off right now?", ["Bluetooth", "bluetooth"]),
        Case("app_automation", "app_automation", "What windows are open right now?", []),
        Case("tool_builder", "tool_builder", "Do you already have a tool that counts words? Just check, do not build one.", []),
    ]  # fmt: skip


def run_case(backend: Backend, case: Case, health: dict[str, bool | None]) -> Result:
    if case.needs and health.get(case.needs) is not True:
        return Result(
            case.name,
            case.agent or "router",
            "SKIP",
            0.0,
            f"{case.needs} not OK on the Health page",
        )
    t0 = time.perf_counter()
    try:
        body = backend.call(
            "POST",
            "/chat",
            {"messages": [{"role": "user", "content": case.message}]},
            timeout=CASE_TIMEOUT_S,
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:120]
        # The PC itself was short of RAM/disk: the agent never ran, so it is not its failure.
        blocked = exc.code == 503 and "low on resources" in detail
        return Result(
            case.name,
            case.agent or "router",
            "BLOCKED" if blocked else "FAIL",
            time.perf_counter() - t0,
            f"HTTP {exc.code}: {detail}",
        )
    except (urllib.error.URLError, OSError) as exc:
        return Result(
            case.name,
            case.agent or "router",
            "FAIL",
            time.perf_counter() - t0,
            f"no answer ({type(exc).__name__})",
        )
    secs = time.perf_counter() - t0
    got = body.get("agents") or []
    reply = body.get("reply", "")
    problems = []
    if case.agent and (not got or got[0] != case.agent):
        problems.append(f"routed to {got or 'router'}")
    if not case.agent and got:
        problems.append(f"expected the Router to answer, went to {got}")
    if body.get("status") != "done":
        problems.append(f"status {body.get('status')}")
    # The card says answers must come from a tool (requires_tool): 0 tool calls = not grounded.
    if case.agent in TOOL_REQUIRED and not body.get("tool_calls"):
        problems.append("0 tool calls although a tool was required (answer not grounded)")
    if reply.startswith("I could not do that"):
        problems.append(reply[:90])
    if case.expect_any and not any(w in reply for w in case.expect_any):
        problems.append(f"reply lacks {case.expect_any[0]!r}")
    note = "; ".join(problems) or reply.replace("\n", " ")[:70]
    return Result(case.name, "→".join(got) or "router", "FAIL" if problems else "PASS", secs, note, reply)  # fmt: skip


def voice_case() -> Result:
    """Offline voice round trip: MMS speaks a sentence, Whisper small transcribes it."""
    t0 = time.perf_counter()
    try:
        import numpy as np

        from voice.config import load_voice_config
        from voice.stt.whisper_stt import SpeechToText
        from voice.tts.speech import Speaker

        cfg = load_voice_config()
        speaker = Speaker(cfg.models_path(), cfg.speak_max_chars, play=lambda a, r: None)
        audio, rate = speaker.synthesize("Where is my invoice file?", "en")
        idx = np.linspace(0, len(audio) - 1, int(len(audio) * 16_000 / rate))
        audio16 = np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)
        stt = SpeechToText(cfg.stt_model, cfg.models_path(), cfg.max_transcribe_seconds)
        stt.load_in_background()
        text = stt.transcribe(audio16, "en")
        stt.unload()
    except Exception as exc:  # noqa: BLE001 - reported in the table
        return Result("voice", "stt+tts", "FAIL", time.perf_counter() - t0, type(exc).__name__)
    ok = "invoice" in text.lower()
    return Result(
        "voice", "stt+tts", "PASS" if ok else "FAIL", time.perf_counter() - t0, f"heard: {text}"
    )


def risky_tool_runs(since: datetime) -> list[str]:
    """T3/T4 tool calls (sends, deletes, moves, settings, clicks) that ran since the test
    started. Must be none. T2 work inside workspace/ (e.g. run_tests) is allowed."""
    path = resolve_audit_path()
    if not path.exists():
        return []
    bad = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("event") != "tool_call" or rec.get("outcome") != "ok":
            continue
        if datetime.fromisoformat(rec["ts"]) < since:
            continue
        reason = rec.get("reason") or ""
        approved = "owner approved" in reason  # a T3/T4 run is logged with its approval id
        if approved or any(f"tier {t}" in reason for t in ("T3", "T4")):
            bad.append(f"{rec.get('agent')}:{rec.get('tool')} ({reason})")
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--only", help="comma-separated case names")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001, S110 - console keeps its encoding
        pass

    backend = Backend()
    if not backend.up():
        print("HCWMA is not running. Start it first:  python -m app.desktop")
        return 2
    started = datetime.now(timezone.utc)
    folder = REPO / "workspace" / f"smoke_test_{datetime.now():%Y%m%d_%H%M%S}"
    files = make_files(folder)
    time.sleep(5)  # the PC file index watcher picks up the new files
    health = {
        i["name"]: i["ok"] for i in backend.call("GET", "/health/details", timeout=60)["items"]
    }
    wanted = set(args.only.split(",")) if args.only else None
    todo: list[Callable[[], Result]] = [
        (lambda c=c: run_case(backend, c, health))
        for c in cases(files)
        if not wanted or c.name in wanted
    ]
    if not wanted or "voice" in wanted:
        todo.append(voice_case)

    results: list[Result] = []
    with ApprovalGuard(backend) as guard:
        for job in todo:
            r = job()
            results.append(r)
            print(f"{r.outcome:<7} {r.case:<17} {r.seconds:6.1f} s  {r.note}", flush=True)
    risky = risky_tool_runs(started)
    replies = folder / "replies.json"  # full replies, for reading answer quality
    replies.write_text(
        json.dumps({r.case: r.reply for r in results}, ensure_ascii=False, indent=1), "utf-8"
    )
    print(f"Full replies: {replies}")

    total = sum(r.seconds for r in results)
    print("\n| Case | Agent | Result | Time | Note |\n|---|---|---|---|---|")
    for r in results:
        print(f"| {r.case} | {r.agent} | {r.outcome} | {r.seconds:.0f} s | {r.note} |")
    counts = {k: sum(r.outcome == k for r in results) for k in ("PASS", "FAIL", "SKIP", "BLOCKED")}
    print(
        f"\n{counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped, "
        f"{counts['BLOCKED']} blocked by low RAM/disk in {total / 60:.1f} min"
    )
    print(f"Approval prompts denied by the smoke test: {guard.denied or 'none'}")
    print(f"T3/T4 tools that ran: {risky or 'none'}")
    safe = not guard.denied and not risky
    return 0 if counts["FAIL"] == 0 and counts["BLOCKED"] == 0 and safe else 1


if __name__ == "__main__":
    raise SystemExit(main())
