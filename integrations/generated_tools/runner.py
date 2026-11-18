"""Runs INSIDE the child process that executes one approved generated tool (build step 13).

Not imported by HCWMA. `executor.py` starts it as
`python -I -B sandbox_guard.py script runner.py` (so the in-process guard is already
installed: no files outside its folder, no network, no new processes). Standard library only.

Protocol, one JSON object per line:
  in : {"files": {name: base64}, "approved_hash": "<sha256>", "args": {...}}   (first line)
  out: {"fetch": "<url>"}   -> in: {"ok": true, "status": 200, ...} or {"ok": false, "error": ".."}
  out: {"result": {...}}    or    {"error": "<short message>"}

Before it executes anything the runner re-checks, itself: (1) the SHA-256 of the files it was
sent equals the hash the owner approved, and (2) the static check passes again on that exact
code. Either one failing means nothing runs.

The adapter source runs with a small allow-list of builtins and an `__import__` that only
admits the allowed modules. Whatever the tool prints is thrown away so it cannot disturb the
protocol. The host treats the reply as untrusted data and validates it again.
"""

from __future__ import annotations

import base64
import builtins
import json
import sys

import static_check  # copied next to this file by executor.py; stdlib only

ALLOWED = frozenset(
    {
        "base64", "bisect", "calendar", "collections", "copy", "dataclasses", "datetime",
        "decimal", "difflib", "enum", "fractions", "functools", "hashlib", "heapq", "html",
        "itertools", "json", "math", "numbers", "operator", "random", "re", "statistics",
        "string", "textwrap", "time", "typing", "unicodedata", "urllib.parse",
    }
)  # fmt: skip
SAFE_NAMES = (
    "abs all any ascii bin bool bytearray bytes callable chr complex dict divmod enumerate "
    "filter float format frozenset hash hex id int isinstance issubclass iter len list map max "
    "min next oct ord pow print range repr reversed round set slice sorted str sum tuple zip "
    "object type super property staticmethod classmethod "
    "ArithmeticError AssertionError AttributeError Exception IndexError KeyError LookupError "
    "NotImplementedError OverflowError RuntimeError StopIteration TypeError ValueError "
    "ZeroDivisionError UnicodeError "
    "True False None NotImplemented Ellipsis __build_class__"
).split()
MAX_FETCH_CALLS = 5


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
    top = name.split(".")[0]
    ok = not level and (
        name in ALLOWED
        or (top in ALLOWED and top != "urllib")
        or (name == "urllib" and tuple(fromlist or ()) == ("parse",))
    )
    if not ok:
        raise ImportError(f"import of '{name}' is not allowed")
    return builtins.__import__(name, globals, locals, fromlist, level)


class _Context:
    """The only thing a tool gets besides its arguments."""

    def __init__(self, send, receive) -> None:
        self._send, self._receive, self._calls = send, receive, 0

    def fetch(self, url):
        """Read one web page from a host the manifest declares. Returns a dict with
        `status`, `final_url` and `text`; raises on a refusal."""
        self._calls += 1
        if self._calls > MAX_FETCH_CALLS:
            raise RuntimeError("too many web requests in one call")
        self._send({"fetch": str(url)[:2000]})
        reply = self._receive()
        if not reply.get("ok"):
            raise RuntimeError(str(reply.get("error", "the request was refused"))[:200])
        return {k: reply[k] for k in ("status", "final_url", "text") if k in reply}


def main() -> int:
    channel_out, channel_in = sys.stdout, sys.stdin

    def send(obj: dict) -> None:
        channel_out.write(json.dumps(obj, ensure_ascii=False) + "\n")
        channel_out.flush()

    def receive() -> dict:
        line = channel_in.readline()
        if not line:
            raise RuntimeError("the host closed the connection")
        return json.loads(line)

    try:
        request = receive()
        files = {n: base64.b64decode(v) for n, v in request["files"].items()}
        if static_check.content_hash(files) != request["approved_hash"]:
            raise PermissionError("the tool's files do not match the version the owner approved")
        source = files["adapter.py"].decode("utf-8")
        problems = static_check.check_tool(source, files["test_adapter.py"].decode("utf-8"))
        if problems:
            raise PermissionError("the static check failed again: " + "; ".join(problems[:2]))
        args = request["args"]
        # Everything the tool prints is dropped; the protocol owns the real stdout.
        sys.stdout = sys.stderr = _Sink()
        namespace: dict = {"__name__": "adapter"}
        namespace["__builtins__"] = {
            **{n: getattr(builtins, n) for n in SAFE_NAMES if hasattr(builtins, n)},
            "__import__": _guarded_import,
        }
        exec(compile(source, "adapter.py", "exec"), namespace)  # noqa: S102
        result = namespace["run"](args, _Context(send, receive))
        if not isinstance(result, dict):
            raise TypeError("run() must return a dict")
        payload = {"result": result}
        json.dumps(payload)
    except BaseException as exc:  # noqa: BLE001 - every failure becomes a short message
        payload = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    sys.stdout = channel_out
    send(payload)
    return 0


class _Sink:
    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        return None


if __name__ == "__main__":
    sys.exit(main())
