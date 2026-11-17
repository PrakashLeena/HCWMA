"""Tools for the Coding and Debugging agents (build step 11; master document 4.3, 6.5, 9.4):
`read_file`, `write_file`, `apply_patch`, `run_in_sandbox`, `run_tests`, `read_logs`.

All six work only inside the coding workspace, `workspace/code/`, a stricter area than the
whole HCWMA workspace (so the agents never touch the File agent's demo files, the memory
demo or anything else in `workspace/`). Rules that hold for every tool here:

- Every path is resolved (symlinks and `..` followed) and passes `PathPolicy.check(...,
  "write")` (rule 10: protected paths, blocked folders) and must then be inside the code
  root. Relative paths are relative to the code root. `.hcwma/` (temp files and run logs)
  is read-only to the agent, except through `read_logs`.
- Nothing is deleted and nothing outside the code root is ever written (rule 9, section 9.3).
- Code runs only through `sandbox_runner.run` (python script / pytest, no shell, scrubbed
  environment, timeout, memory and output caps, in-process guard).
- File text and program output are data (rule 13). Results are small: the local model reads
  them on a CPU.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from integrations.adapters.pc import sandbox_runner
from integrations.mcp.tool_schemas import (
    PermissionTier,
    ToolManifest,
    ToolPermissions,
    ToolSpec,
    ToolStatus,
)
from security.permissions.path_check import PathPolicy

CODE_DIR = "code"
MAX_FILE_BYTES = 200_000
MAX_READ_LINES = 400
DEFAULT_READ_LINES = 200
MAX_OUTPUT_CHARS = 3000
MAX_LISTING = 100
SKIP_NAMES = {"__pycache__", ".pytest_cache", sandbox_runner.INTERNAL_DIR}
# Files that could run something if the owner opened them. Python source is fine: it only
# runs through the sandbox.
BLOCKED_SUFFIXES = {
    ".exe", ".dll", ".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".wsf", ".msi", ".lnk", ".scr",
    ".com", ".sh", ".reg", ".jar", ".pif", ".hta", ".cpl", ".so", ".dylib", ".sys", ".pyd",
}  # fmt: skip

# Owner approved all six manifests on 2026-11-17 (add-tool skill, section 4). A new or
# changed coding tool starts as "quarantined" again until the owner has seen it.
CODE_TOOLS_STATUS: ToolStatus = "approved"
_CODE_SCOPE = ToolPermissions(filesystem=["workspace/code"])


class ToolRefused(RuntimeError):
    """The action cannot be done as asked. The message is safe to show to the agent."""


@dataclass(frozen=True)
class CodeToolDeps:
    policy: PathPolicy

    @property
    def code_root(self) -> Path:
        root = self.policy.workspace / CODE_DIR
        root.mkdir(parents=True, exist_ok=True)
        return root.resolve()


# ---------- paths ----------


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or path.is_relative_to(folder)


def _resolve(deps: CodeToolDeps, path: str, *, writing: bool = False) -> Path:
    root = deps.code_root
    raw = Path(path)
    if not raw.is_absolute():
        raw = root / raw
    real = deps.policy.check(raw, "write")  # protected / blocked / outside workspace
    if not _inside(real, root):
        raise ToolRefused(f"'{path}' is outside the coding workspace; only code/ is allowed")
    internal = root / sandbox_runner.INTERNAL_DIR
    if writing and _inside(real, internal):
        raise ToolRefused("the .hcwma folder is managed by HCWMA; write somewhere else")
    if real == root and writing:
        raise ToolRefused("give a file name, not the workspace folder itself")
    return real


def _show(deps: CodeToolDeps, path: Path) -> str:
    return path.relative_to(deps.code_root).as_posix() or "."


def _check_suffix(path: Path) -> None:
    if path.suffix.lower() in BLOCKED_SUFFIXES:
        raise ToolRefused(
            f"files of type '{path.suffix}' are not allowed; write source code or text files"
        )


def _read_text(path: Path) -> tuple[str, str]:
    """(text with \\n newlines, newline style of the file)."""
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ToolRefused(f"'{path.name}' is larger than {MAX_FILE_BYTES // 1000} KB")
    raw = path.read_bytes()
    if b"\x00" in raw[:2048]:
        raise ToolRefused(f"'{path.name}' is a binary file, not text")
    text = raw.decode("utf-8", errors="replace")
    style = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), style


def _write_text(path: Path, text: str, style: str = "\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\n", style).encode("utf-8"))


def _clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Keep the start and (more of) the end: that is where tracebacks and summaries are."""
    if len(text) <= limit:
        return text
    head = limit // 4
    left_out = len(text) - limit
    return f"{text[:head]}\n... [{left_out} characters left out] ...\n{text[-(limit - head) :]}"


# ---------- read_file ----------


class ReadFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=300, description="File or folder, e.g. src/app.py.")
    start_line: int = Field(default=1, ge=1, description="First line to return (1 = the top).")
    max_lines: int = Field(default=DEFAULT_READ_LINES, ge=1, le=MAX_READ_LINES)


class ReadFileOutput(BaseModel):
    kind: str  # "file" | "folder"
    path: str
    content: str | None = None
    total_lines: int | None = None
    truncated: bool = False
    entries: list[str] | None = None
    note: str | None = None


def _read_file(deps: CodeToolDeps, args: ReadFileInput) -> ReadFileOutput:
    real = _resolve(deps, args.path)
    shown = _show(deps, real)
    if not real.exists():
        raise ToolRefused(f"there is no file or folder '{shown}' in the coding workspace")
    if real.is_dir():
        names = sorted(
            (f"{p.name}/" if p.is_dir() else p.name)
            for p in real.iterdir()
            if p.name not in SKIP_NAMES
        )
        note = None if len(names) <= MAX_LISTING else f"only the first {MAX_LISTING} are shown"
        return ReadFileOutput(kind="folder", path=shown, entries=names[:MAX_LISTING], note=note)
    text, _ = _read_text(real)
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    window = lines[args.start_line - 1 : args.start_line - 1 + args.max_lines]
    end = args.start_line - 1 + len(window)
    return ReadFileOutput(
        kind="file",
        path=shown,
        content="\n".join(window),
        total_lines=len(lines),
        truncated=end < len(lines),
        note=None
        if end >= len(lines)
        else f"more lines follow; call again with start_line={end + 1}",
    )


# ---------- write_file ----------


class WriteFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=300, description="New file, e.g. calc/main.py.")
    content: str = Field(max_length=MAX_FILE_BYTES, description="The whole text of the file.")
    overwrite: bool = Field(
        default=False, description="true to replace a file that already exists."
    )


class WriteFileOutput(BaseModel):
    path: str
    created: bool
    lines: int


def _write_file(deps: CodeToolDeps, args: WriteFileInput) -> WriteFileOutput:
    real = _resolve(deps, args.path, writing=True)
    _check_suffix(real)
    exists = real.exists()
    if exists and real.is_dir():
        raise ToolRefused(f"'{_show(deps, real)}' is a folder")
    if exists and not args.overwrite:
        raise ToolRefused(
            f"'{_show(deps, real)}' already exists. Use apply_patch for a small change, or "
            "call write_file again with overwrite=true to replace the whole file."
        )
    style = "\n"
    if exists:
        _, style = _read_text(real)
    _write_text(real, args.content, style)
    return WriteFileOutput(
        path=_show(deps, real), created=not exists, lines=args.content.count("\n") + 1
    )


# ---------- apply_patch ----------


class Edit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    old: str = Field(min_length=1, max_length=20_000, description="Text now in the file.")
    new: str = Field(max_length=20_000, description="Text to put in its place ('' deletes).")


class ApplyPatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=300)
    edits: list[Edit] = Field(min_length=1, max_length=10)


class ApplyPatchOutput(BaseModel):
    path: str
    edits_applied: int
    diff: str


def _apply_patch(deps: CodeToolDeps, args: ApplyPatchInput) -> ApplyPatchOutput:
    real = _resolve(deps, args.path, writing=True)
    shown = _show(deps, real)
    if not real.is_file():
        raise ToolRefused(f"there is no file '{shown}' to patch; use write_file to create it")
    _check_suffix(real)
    original, style = _read_text(real)
    text = original
    for number, edit in enumerate(args.edits, start=1):
        old, new = edit.old.replace("\r\n", "\n"), edit.new.replace("\r\n", "\n")
        count = text.count(old)
        if count == 0:
            raise ToolRefused(
                f"edit {number}: the 'old' text is not in {shown}. Nothing was changed. Call "
                "read_file again and copy the lines exactly, including the indentation."
            )
        if count > 1:
            raise ToolRefused(
                f"edit {number}: the 'old' text appears {count} times in {shown}. Nothing was "
                "changed. Include more surrounding lines so that it matches exactly once."
            )
        text = text.replace(old, new, 1)
    _write_text(real, text, style)
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            text.splitlines(keepends=True),
            fromfile=f"a/{shown}",
            tofile=f"b/{shown}",
            n=1,
        )
    )
    return ApplyPatchOutput(path=shown, edits_applied=len(args.edits), diff=_clip(diff, 2000))


# ---------- run_in_sandbox ----------


class RunInSandboxInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(
        min_length=1,
        max_length=300,
        description="'python script.py [args]' or 'python -m pytest [args]'. Use / in paths.",
    )
    cwd: str = Field(default=".", max_length=300, description="Folder to run in.")
    timeout_s: int = Field(default=30, ge=1, le=120)


class RunOutput(BaseModel):
    exit_code: int | None
    stopped: str | None = None
    seconds: float
    output: str
    log: str
    note: str | None = None


def _run(
    deps: CodeToolDeps,
    command: str,
    cwd: str,
    timeout_s: float,
    kind: str,
    extra_args: list[str] | None = None,
) -> RunOutput:
    folder = _resolve(deps, cwd)
    try:
        result = sandbox_runner.run(
            command,
            code_root=deps.code_root,
            cwd=folder,
            timeout_s=timeout_s,
            kind=kind,
            extra_args=extra_args,
        )
    except sandbox_runner.CommandRefused as exc:
        raise ToolRefused(str(exc)) from exc
    note = None
    if result.stopped == "timeout":
        note = f"stopped after {timeout_s:g}s; the program may loop forever or wait for input"
    elif result.stopped:
        note = f"stopped: {result.stopped.replace('_', ' ')}"
    elif "HCWMA sandbox:" in result.output:
        note = "the sandbox blocked an action outside code/ (no network, no new processes)"
    return RunOutput(
        exit_code=result.exit_code,
        stopped=result.stopped,
        seconds=result.seconds,
        output=_clip(result.output.strip()),
        log=result.log_path.relative_to(deps.code_root).as_posix(),
        note=note,
    )


def _run_in_sandbox(deps: CodeToolDeps, args: RunInSandboxInput) -> RunOutput:
    return _run(deps, args.command, args.cwd, args.timeout_s, "run")


# ---------- run_tests ----------


class RunTestsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(default=".", max_length=300, description="Project folder or test file.")
    keyword: str | None = Field(default=None, max_length=100, description="Only tests matching.")
    timeout_s: int = Field(default=60, ge=1, le=120)


class RunTestsOutput(RunOutput):
    passed: int | None = None
    failed: int | None = None
    errors: int | None = None
    summary: str | None = None


_COUNT = re.compile(r"(\d+) (passed|failed|error|errors)\b")


def _run_tests(deps: CodeToolDeps, args: RunTestsInput) -> RunTestsOutput:
    target = _resolve(deps, args.path)
    if not target.exists():
        raise ToolRefused(f"there is no file or folder '{_show(deps, target)}'")
    folder = target if target.is_dir() else target.parent
    internal = sandbox_runner.internal_dir(deps.code_root)
    internal.mkdir(parents=True, exist_ok=True)
    config = internal / "pytest.ini"  # an empty config: pytest must not read ini files above code/
    config.write_text("[pytest]\n", encoding="utf-8")
    extra = ["-q", "--tb=short", "--no-header", "-p", "no:cacheprovider"]
    extra += ["-c", str(config), "--rootdir", str(folder), "--basetemp", str(internal / "pytest")]
    if args.keyword:
        extra += ["-k", args.keyword]
    if target.is_file():
        extra.append(target.name)
    base = _run(deps, "python -m pytest", _show(deps, folder), args.timeout_s, "tests", extra)
    counts = {kind: 0 for kind in ("passed", "failed", "error")}
    last = next((ln for ln in reversed(base.output.splitlines()) if _COUNT.search(ln)), None)
    for number, kind in _COUNT.findall(last or ""):
        counts["error" if kind.startswith("error") else kind] += int(number)
    found = last is not None
    return RunTestsOutput(
        **base.model_dump(),
        passed=counts["passed"] if found else None,
        failed=counts["failed"] if found else None,
        errors=counts["error"] if found else None,
        summary=last.strip(" =") if last else "no tests were collected or pytest did not finish",
    )


# ---------- read_logs ----------


class ReadLogsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str | None = Field(
        default=None,
        max_length=300,
        description="A .log or .txt file, or a log name from a run. Empty = the latest run.",
    )
    tail_lines: int = Field(default=60, ge=1, le=300)
    contains: str | None = Field(default=None, max_length=100, description="Only lines with this.")


class ReadLogsOutput(BaseModel):
    log: str
    lines_total: int
    content: str
    recent_logs: list[str]
    note: str | None = None


def _read_logs(deps: CodeToolDeps, args: ReadLogsInput) -> ReadLogsOutput:
    logs_dir = sandbox_runner.internal_dir(deps.code_root) / "logs"
    recent = sorted(logs_dir.glob("*.log"), reverse=True) if logs_dir.is_dir() else []
    if args.path:
        candidate = args.path
        if "/" not in candidate and "\\" not in candidate and (logs_dir / candidate).is_file():
            candidate = str(logs_dir / candidate)
        real = _resolve(deps, candidate)
        if real.suffix.lower() not in {".log", ".txt", ".out"}:
            raise ToolRefused("read_logs only opens .log, .txt and .out files; use read_file")
    elif recent:
        real = recent[0]
    else:
        raise ToolRefused("there are no run logs yet; run the code or the tests first")
    if not real.is_file():
        raise ToolRefused(f"there is no log '{_show(deps, real)}'")
    with real.open("rb") as handle:  # logs can be big: only the end matters
        size = handle.seek(0, 2)
        handle.seek(max(0, size - MAX_FILE_BYTES))
        text = handle.read().decode("utf-8", errors="replace").replace("\r\n", "\n")
    lines = text.split("\n")
    if args.contains:
        lines = [ln for ln in lines if args.contains.lower() in ln.lower()]
    shown = lines[-args.tail_lines :]
    return ReadLogsOutput(
        log=_show(deps, real),
        lines_total=len(lines),
        content=_clip("\n".join(shown).strip()),
        recent_logs=[p.name for p in recent[:5]],
        note=None if len(shown) == len(lines) else f"the last {len(shown)} lines are shown",
    )


# ---------- registration ----------


def code_tool_specs(deps: CodeToolDeps, status: ToolStatus = "quarantined") -> list[ToolSpec]:
    def spec(
        name: str,
        description: str,
        tier: PermissionTier,
        input_model: type[BaseModel],
        output_model: type[BaseModel],
        handler: object,
        *,
        writes: bool,
        timeout_s: float = 20,
    ) -> ToolSpec:
        manifest = ToolManifest(
            name=name,
            version="0.1.0",
            created_by="developer",
            status=status,
            route="pc",
            description=description,
            permission_tier=tier,
            side_effects="writes" if writes else "none",
            permissions=_CODE_SCOPE,
            timeout_s=timeout_s,
        )
        return ToolSpec(
            manifest=manifest,
            input_model=input_model,
            output_model=output_model,
            handler=handler,  # type: ignore[arg-type]
        )

    return [
        spec(
            "read_file",
            "Read a text file (or list a folder) in the coding workspace. Paths are relative "
            "to the workspace. Returns at most 200 lines unless asked for more.",
            PermissionTier.T1,
            ReadFileInput,
            ReadFileOutput,
            lambda a: _read_file(deps, a),
            writes=False,
        ),
        spec(
            "write_file",
            "Create a text or source file in the coding workspace. Refuses to replace an "
            "existing file unless overwrite=true. Cannot write outside the workspace.",
            PermissionTier.T2,
            WriteFileInput,
            WriteFileOutput,
            lambda a: _write_file(deps, a),
            writes=True,
        ),
        spec(
            "apply_patch",
            "Change an existing file in the coding workspace by replacing exact pieces of "
            "text (old -> new). All edits apply or none do; each 'old' must match once.",
            PermissionTier.T2,
            ApplyPatchInput,
            ApplyPatchOutput,
            lambda a: _apply_patch(deps, a),
            writes=True,
        ),
        spec(
            "run_in_sandbox",
            "Run 'python script.py' or 'python -m pytest' inside the coding workspace with "
            "no network, no new processes, a time limit and no access outside the workspace.",
            PermissionTier.T2,
            RunInSandboxInput,
            RunOutput,
            lambda a: _run_in_sandbox(deps, a),
            writes=True,
            timeout_s=150,
        ),
        spec(
            "run_tests",
            "Run pytest on a project folder or test file in the coding workspace, in the "
            "sandbox, and report passed / failed counts and the failure output.",
            PermissionTier.T2,
            RunTestsInput,
            RunTestsOutput,
            lambda a: _run_tests(deps, a),
            writes=True,
            timeout_s=150,
        ),
        spec(
            "read_logs",
            "Show the end of a run log (the latest by default) or a .log/.txt file in the "
            "coding workspace, optionally only lines containing a word.",
            PermissionTier.T1,
            ReadLogsInput,
            ReadLogsOutput,
            lambda a: _read_logs(deps, a),
            writes=False,
        ),
    ]


def default_code_tool_specs() -> list[ToolSpec]:
    """The coding tools wired to `configs/permissions.yaml`."""
    return code_tool_specs(CodeToolDeps(PathPolicy.from_config()), status=CODE_TOOLS_STATUS)
