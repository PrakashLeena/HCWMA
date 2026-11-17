"""In-process guard for code the coding agents run (build step 11; master document 6.1, 9.3).

This file is NOT imported by HCWMA. `sandbox_runner.py` starts a fresh `python -I -B` child
process with this file as the script:

    python -I -B -X utf8 sandbox_guard.py script <file.py> [args...]
    python -I -B -X utf8 sandbox_guard.py module <pytest|unittest> [args...]

The guard installs a Python audit hook (PEP 578) and then runs the target. While it runs:

- files may be written, created, renamed or deleted only inside the sandbox root;
- files may be read only inside the sandbox root and the Python installation itself;
- no network (sockets, DNS), no new processes, no `os.startfile`. (ctypes is not blocked:
  pytest's colorama needs it on Windows.)

Honest limit: this is a guardrail against code that is wrong or careless, which is what a
small local model writes. It is not a jail. Deliberately hostile code can get around an
in-process hook; Windows has no container available here, so the real protection is the
rest of the design: only `python script` / `python -m pytest` can be started, only inside
`workspace/code/`, with a scrubbed environment, a timeout and a memory/output cap, and the
agents read the result as data (architecture rules 1, 10 and 13).
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

ROOT_ENV = "HCWMA_SANDBOX_ROOT"
ALLOWED_MODULES = ("pytest", "unittest")

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
_BLOCKED_PREFIXES = (
    "socket.",
    "subprocess.",
    "os.exec",
    "os.spawn",
    "os.posix_spawn",
    "os.fork",
    "os.system",
    "os.startfile",
    "os.kill",
    "webbrowser.",
    "winreg.",
)
# events whose path-like arguments are modified (every one must be inside the root)
_WRITE_EVENTS = frozenset(
    {
        "os.remove",
        "os.rename",
        "os.rmdir",
        "os.mkdir",
        "os.chmod",
        "os.chown",
        "os.truncate",
        "os.symlink",
        "os.link",
        "os.utime",
        "shutil.copyfile",
        "shutil.copymode",
        "shutil.copystat",
        "shutil.copytree",
        "shutil.move",
        "shutil.rmtree",
        "shutil.make_archive",
        "shutil.unpack_archive",
        "tempfile.mkdtemp",
        "tempfile.mkstemp",
    }
)
_READ_EVENTS = frozenset({"os.listdir", "os.scandir", "glob.glob", "glob.glob/2"})
_NULL_DEVICES = {"nul", "/dev/null", os.devnull.lower()}


def _real(path: object) -> str | None:
    """A normalised absolute path for a path-like value, or None for fds and other values."""
    if isinstance(path, int) or path is None:
        return None
    try:
        text = os.fsdecode(path)  # type: ignore[arg-type]
    except TypeError:
        return None
    lowered = text.lower()
    if not text or lowered in _NULL_DEVICES or lowered in ("\\\\.\\nul", "//./nul"):
        return None
    return os.path.normcase(os.path.realpath(text))


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("\\/") + os.sep)


def install(root: Path) -> None:
    sandbox = os.path.normcase(os.path.realpath(root))
    read_ok = [sandbox]
    for prefix in {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix}:
        read_ok.append(os.path.normcase(os.path.realpath(prefix)))

    def deny(event: str, path: object) -> None:
        raise PermissionError(f"HCWMA sandbox: {event} is not allowed on '{path}' (outside code/)")

    def check_write(event: str, path: object) -> None:
        real = _real(path)
        if real is not None and not _inside(real, sandbox):
            deny(event, path)

    def check_read(event: str, path: object) -> None:
        real = _real(path)
        if real is not None and not any(_inside(real, ok) for ok in read_ok):
            deny(event, path)

    def hook(event: str, args: tuple[object, ...]) -> None:
        if event == "open":
            path, _mode, flags = args[0], args[1], args[2]
            if isinstance(flags, int) and flags & _WRITE_FLAGS:
                check_write("write", path)
            else:
                check_read("read", path)
        elif event in _WRITE_EVENTS:
            for value in args:
                check_write(event, value)
        elif event in _READ_EVENTS:
            check_read(event, args[0])
        elif event.startswith(_BLOCKED_PREFIXES):
            raise PermissionError(
                f"HCWMA sandbox: '{event}' is not allowed (no network, no new processes)"
            )

    sys.addaudithook(hook)


def main(argv: list[str]) -> int:
    root = os.environ.get(ROOT_ENV)
    if not root or len(argv) < 3 or argv[1] not in ("script", "module"):
        print("sandbox_guard: bad arguments", file=sys.stderr)
        return 2
    mode, target, rest = argv[1], argv[2], argv[3:]
    if mode == "module" and target not in ALLOWED_MODULES:
        print(f"sandbox_guard: module '{target}' is not allowed", file=sys.stderr)
        return 2
    install(Path(root))
    cwd = os.getcwd()
    if mode == "script":
        sys.argv = [target, *rest]
        sys.path.insert(0, os.path.dirname(os.path.abspath(target)))
        runpy.run_path(target, run_name="__main__")
    else:
        sys.argv = [target, *rest]
        sys.path.insert(0, cwd)
        runpy.run_module(target, run_name="__main__", alter_sys=True)
    return 0


if __name__ == "__main__":
    try:
        code = main(sys.argv)
    except SystemExit:
        raise
    sys.exit(code)
