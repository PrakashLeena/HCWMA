"""Static check for code written by the Tool Builder (build step 13; owner rules, 2026-11-18).

Runs before the sandbox and again before every approval. It reads the code with `ast` and
never runs it. v1 tools are pure, read-only functions, so the rule is an allow-list:

- imports: only a short list of harmless standard-library modules (no `os`, `subprocess`,
  `ctypes`, `socket`, `shutil`, `importlib`, `sys`, `io`, `pathlib`, ...);
- no `eval`, `exec`, `compile`, `open`, `__import__`, `getattr`/`setattr`, `globals`...:
  not even named, so they cannot be passed around;
- no attribute starting with `__`, no string containing `__`, no `.format()` (format strings
  can read attributes), so the usual ways out of a restricted function are closed;
- `adapter.py` must define `run(args, ctx)` at module level.

Honest limit: a static check stops careless and obvious code, not a determined attacker. It
is one layer of several: tests run only in a throw-away Windows Sandbox, the owner reads the
full code before approving, an approved tool runs in a separate process with the in-process
guard, restricted builtins and a time limit, and a changed byte sends it back to quarantine.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

ALLOWED_IMPORTS = frozenset(
    {
        "base64", "bisect", "calendar", "collections", "copy", "dataclasses", "datetime",
        "decimal", "difflib", "enum", "fractions", "functools", "hashlib", "heapq", "html",
        "itertools", "json", "math", "numbers", "operator", "random", "re", "statistics",
        "string", "textwrap", "time", "typing", "unicodedata",
    }
)  # fmt: skip
# `urllib.parse` only (building and splitting addresses); `urllib.request` is not allowed.
ALLOWED_FROM = frozenset({"urllib.parse"})
TEST_ONLY_IMPORTS = frozenset({"unittest", "adapter"})

# Named in the owner's rules: give a clear reason when the code uses one of these.
FORBIDDEN_IMPORTS = frozenset(
    {
        "os", "subprocess", "ctypes", "socket", "shutil", "importlib", "sys", "io", "pathlib",
        "builtins", "pickle", "marshal", "multiprocessing", "threading", "asyncio", "tempfile",
        "glob", "http", "urllib", "requests", "httpx", "ssl", "winreg", "webbrowser", "inspect",
        "gc", "signal", "code", "runpy", "sqlite3", "keyring", "zipfile", "tarfile", "ftplib",
        "smtplib", "platform", "pty", "fcntl", "select", "mmap", "types", "traceback",
    }
)  # fmt: skip
FORBIDDEN_NAMES = frozenset(
    {
        "eval", "exec", "compile", "open", "__import__", "input", "breakpoint", "globals",
        "locals", "vars", "getattr", "setattr", "delattr", "dir", "help", "exit", "quit",
        "memoryview", "__builtins__", "__loader__", "__spec__",
    }
)  # fmt: skip
FORBIDDEN_METHODS = frozenset({"format", "format_map"})


@dataclass(frozen=True)
class Problem:
    line: int
    message: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.message}"


class _Visitor(ast.NodeVisitor):
    def __init__(self, *, is_test: bool) -> None:
        self.problems: list[Problem] = []
        self.allowed = ALLOWED_IMPORTS | (TEST_ONLY_IMPORTS if is_test else frozenset())

    def bad(self, node: ast.AST, message: str) -> None:
        self.problems.append(Problem(getattr(node, "lineno", 0), message))

    def _import_name(self, node: ast.AST, dotted: str) -> None:
        top = dotted.split(".")[0]
        if dotted in ALLOWED_FROM or top in self.allowed and top != "urllib":
            return
        if top in FORBIDDEN_IMPORTS:
            self.bad(node, f"importing '{dotted}' is not allowed (no files, processes or network)")
        else:
            self.bad(node, f"importing '{dotted}' is not on the allowed list")

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._import_name(node, alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            self.bad(node, "relative imports are not allowed")
        elif node.module == "urllib" and all(a.name == "parse" for a in node.names):
            return  # `from urllib import parse`
        else:
            self._import_name(node, node.module or "")

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in FORBIDDEN_NAMES:
            self.bad(node, f"'{node.id}' is not allowed")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("__"):
            self.bad(node, f"attribute '{node.attr}' is not allowed")
        elif node.attr in FORBIDDEN_METHODS:
            self.bad(node, f"'.{node.attr}()' is not allowed; use an f-string")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and "__" in node.value and node.value != "__main__":
            self.bad(node, "text containing '__' is not allowed")

    def visit_Global(self, node: ast.Global) -> None:
        self.bad(node, "'global' is not allowed")

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.bad(node, "'nonlocal' is not allowed")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node.keywords:
            self.bad(node, "class keywords (metaclass) are not allowed")
        self.generic_visit(node)

    def visit_Await(self, node: ast.Await) -> None:
        self.bad(node, "async code is not allowed")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.bad(node, "async code is not allowed")


def check_source(source: str, *, is_test: bool = False) -> list[Problem]:
    """Problems found in one Python file (empty list = passes the static check)."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [Problem(exc.lineno or 0, f"syntax error: {exc.msg}")]
    visitor = _Visitor(is_test=is_test)
    visitor.visit(tree)
    problems = visitor.problems
    if not is_test:
        runs = [
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "run" and len(n.args.args) == 2
        ]
        if not runs:
            problems.append(Problem(0, "define `def run(args, ctx):` at the top of the file"))
    return sorted(problems, key=lambda p: p.line)


def check_tool(adapter: str, tests: str) -> list[str]:
    """Problems in both code files, as short strings (adapter first)."""
    found = [f"adapter.py {p}" for p in check_source(adapter)]
    found += [f"test_adapter.py {p}" for p in check_source(tests, is_test=True)]
    return found[:12]
