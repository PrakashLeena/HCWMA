"""Step 11: the coding workspace tools and the sandbox (add-tool skill test list).

Everything runs over a throw-away layout in a temp folder; the sandbox tests start real
child processes with the real guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from integrations.adapters.pc import code_tools as ct
from integrations.adapters.pc import sandbox_runner
from integrations.mcp.tool_schemas import ToolSpec
from security.permissions.path_check import PathDenied
from tests.pc_layout import Layout, make_layout


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    return make_layout(tmp_path)


@pytest.fixture
def deps(layout: Layout) -> ct.CodeToolDeps:
    return ct.CodeToolDeps(layout.policy)


def _write(deps: ct.CodeToolDeps, path: str, content: str, **kw: object) -> ct.WriteFileOutput:
    return ct._write_file(deps, ct.WriteFileInput(path=path, content=content, **kw))


def _run(deps: ct.CodeToolDeps, command: str, cwd: str = ".", timeout_s: int = 20) -> ct.RunOutput:
    return ct._run_in_sandbox(
        deps, ct.RunInSandboxInput(command=command, cwd=cwd, timeout_s=timeout_s)
    )


# ---------- manifests ----------


def test_manifests_tiers_and_default_status(deps: ct.CodeToolDeps) -> None:
    specs = {s.name: s for s in ct.code_tool_specs(deps)}
    assert set(specs) == {
        "read_file",
        "write_file",
        "apply_patch",
        "run_in_sandbox",
        "run_tests",
        "read_logs",
    }
    assert {n: s.manifest.permission_tier.value for n, s in specs.items()} == {
        "read_file": "T1",
        "read_logs": "T1",
        "write_file": "T2",
        "apply_patch": "T2",
        "run_in_sandbox": "T2",
        "run_tests": "T2",
    }
    # rule 12 / add-tool skill: a new tool is quarantined until the owner has seen it
    assert all(
        isinstance(s, ToolSpec) and s.manifest.status == "quarantined" for s in specs.values()
    )
    assert all(s.manifest.permissions.filesystem == ["workspace/code"] for s in specs.values())
    assert all(not s.manifest.permissions.network for s in specs.values())


# ---------- write_file / read_file ----------


def test_write_then_read_and_list(deps: ct.CodeToolDeps) -> None:
    out = _write(deps, "calc/main.py", "def add(a, b):\n    return a + b\n")
    assert out.created and out.path == "calc/main.py" and out.lines == 3
    assert (deps.code_root / "calc" / "main.py").is_file()

    read = ct._read_file(deps, ct.ReadFileInput(path="calc/main.py"))
    assert read.kind == "file" and read.total_lines == 2 and "return a + b" in (read.content or "")
    listing = ct._read_file(deps, ct.ReadFileInput(path="calc"))
    assert listing.kind == "folder" and listing.entries == ["main.py"]


def test_read_file_pages_through_long_files(deps: ct.CodeToolDeps) -> None:
    _write(deps, "big.txt", "\n".join(f"line {i}" for i in range(1, 11)))
    page = ct._read_file(deps, ct.ReadFileInput(path="big.txt", max_lines=4))
    assert page.truncated and "start_line=5" in (page.note or "")
    rest = ct._read_file(deps, ct.ReadFileInput(path="big.txt", start_line=9, max_lines=4))
    assert rest.content == "line 9\nline 10" and not rest.truncated


def test_write_never_overwrites_unless_asked(deps: ct.CodeToolDeps) -> None:
    _write(deps, "a.py", "x = 1\n")
    with pytest.raises(ct.ToolRefused, match="already exists"):
        _write(deps, "a.py", "x = 2\n")
    assert (deps.code_root / "a.py").read_text(encoding="utf-8") == "x = 1\n"
    out = _write(deps, "a.py", "x = 2\n", overwrite=True)
    assert not out.created and (deps.code_root / "a.py").read_text(encoding="utf-8") == "x = 2\n"


def test_overwrite_keeps_windows_line_endings(deps: ct.CodeToolDeps) -> None:
    path = deps.code_root / "crlf.py"
    path.write_bytes(b"a = 1\r\nb = 2\r\n")
    _write(deps, "crlf.py", "a = 9\nb = 2\n", overwrite=True)
    assert path.read_bytes() == b"a = 9\r\nb = 2\r\n"


@pytest.mark.parametrize("name", ["run.bat", "setup.ps1", "tool.exe", "x.lnk", "go.sh", "a.DLL"])
def test_executable_file_types_are_refused(deps: ct.CodeToolDeps, name: str) -> None:
    with pytest.raises(ct.ToolRefused, match="not allowed"):
        _write(deps, name, "echo hi")
    assert not (deps.code_root / name).exists()


def test_invalid_arguments_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ct.WriteFileInput(path="a.py", content="x", mode="w")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ct.ReadFileInput(path="")
    with pytest.raises(ValidationError):
        ct.RunInSandboxInput(command="python a.py", timeout_s=9999)
    with pytest.raises(ValidationError):
        ct.ApplyPatchInput(path="a.py", edits=[])


def test_binary_and_huge_files_are_not_read(deps: ct.CodeToolDeps) -> None:
    (deps.code_root / "pic.png").write_bytes(b"\x89PNG\x00\x00data")
    with pytest.raises(ct.ToolRefused, match="binary"):
        ct._read_file(deps, ct.ReadFileInput(path="pic.png"))
    (deps.code_root / "huge.txt").write_text("x" * (ct.MAX_FILE_BYTES + 1), encoding="utf-8")
    with pytest.raises(ct.ToolRefused, match="larger than"):
        ct._read_file(deps, ct.ReadFileInput(path="huge.txt"))


# ---------- nothing outside the workspace (rule 10, master document 9.3) ----------


@pytest.mark.parametrize(
    "bad",
    ["../draft-report.md", "../../escape.py", "..\\..\\escape.py", "sub/../../escape.py"],
)
def test_paths_that_climb_out_are_refused(deps: ct.CodeToolDeps, layout: Layout, bad: str) -> None:
    with pytest.raises((ct.ToolRefused, PathDenied)):
        _write(deps, bad, "x = 1")
    with pytest.raises((ct.ToolRefused, PathDenied)):
        ct._read_file(deps, ct.ReadFileInput(path=bad))
    assert not (layout.repo / "escape.py").exists()
    assert not (layout.workspace / "escape.py").exists()


def test_absolute_paths_outside_code_are_refused(deps: ct.CodeToolDeps, layout: Layout) -> None:
    for target in (layout.outside / "new.py", layout.docs / "notes" / "meeting notes.txt"):
        with pytest.raises((ct.ToolRefused, PathDenied)):
            _write(deps, str(target), "x = 1", overwrite=True)
    with pytest.raises((ct.ToolRefused, PathDenied)):
        ct._read_file(deps, ct.ReadFileInput(path=str(layout.home / ".ssh" / "id_ed25519")))
    # the rest of the workspace is not the coding workspace
    with pytest.raises(ct.ToolRefused, match="outside the coding workspace"):
        ct._read_file(deps, ct.ReadFileInput(path=str(layout.workspace / "draft-report.md")))
    assert not (layout.outside / "new.py").exists()


def test_symlink_that_points_out_is_refused(deps: ct.CodeToolDeps, layout: Layout) -> None:
    link = deps.code_root / "shortcut"
    try:
        link.symlink_to(layout.outside, target_is_directory=True)
    except OSError:  # no symlink privilege on Windows: a junction needs none
        import subprocess

        made = subprocess.run(  # noqa: S603
            ["cmd", "/c", "mklink", "/J", str(link), str(layout.outside)],  # noqa: S607
            capture_output=True,
            check=False,
        )
        if made.returncode != 0:
            pytest.skip("neither symlinks nor junctions can be made on this machine")
    with pytest.raises((ct.ToolRefused, PathDenied)):
        _write(deps, "shortcut/leak.py", "x = 1")
    assert not (layout.outside / "leak.py").exists()


def test_internal_folder_is_not_writable(deps: ct.CodeToolDeps) -> None:
    with pytest.raises(ct.ToolRefused, match=".hcwma"):
        _write(deps, ".hcwma/logs/fake.log", "forged")
    with pytest.raises(ct.ToolRefused, match="file name"):
        _write(deps, ".", "x")


# ---------- apply_patch ----------


def test_patch_replaces_exact_text_and_shows_a_diff(deps: ct.CodeToolDeps) -> None:
    _write(deps, "m.py", "def f(x):\n    return x - 1\n\nprint(f(2))\n")
    out = ct._apply_patch(
        deps,
        ct.ApplyPatchInput(path="m.py", edits=[ct.Edit(old="return x - 1", new="return x + 1")]),
    )
    assert (
        out.edits_applied == 1
        and "-    return x - 1" in out.diff
        and "+    return x + 1" in out.diff
    )
    assert (deps.code_root / "m.py").read_text(encoding="utf-8").count("x + 1") == 1


def test_patch_is_all_or_nothing(deps: ct.CodeToolDeps) -> None:
    _write(deps, "m.py", "a = 1\nb = 2\n")
    with pytest.raises(ct.ToolRefused, match="edit 2"):
        ct._apply_patch(
            deps,
            ct.ApplyPatchInput(
                path="m.py", edits=[ct.Edit(old="a = 1", new="a = 5"), ct.Edit(old="zzz", new="y")]
            ),
        )
    assert (deps.code_root / "m.py").read_text(encoding="utf-8") == "a = 1\nb = 2\n"


def test_patch_needs_a_unique_match(deps: ct.CodeToolDeps) -> None:
    _write(deps, "m.py", "x = 1\nx = 1\n")
    with pytest.raises(ct.ToolRefused, match="2 times"):
        ct._apply_patch(
            deps, ct.ApplyPatchInput(path="m.py", edits=[ct.Edit(old="x = 1", new="x = 2")])
        )


def test_patch_keeps_crlf_and_refuses_a_missing_file(deps: ct.CodeToolDeps) -> None:
    (deps.code_root / "w.py").write_bytes(b"a = 1\r\nb = 2\r\n")
    ct._apply_patch(
        deps, ct.ApplyPatchInput(path="w.py", edits=[ct.Edit(old="a = 1\nb", new="a = 7\nb")])
    )
    assert (deps.code_root / "w.py").read_bytes() == b"a = 7\r\nb = 2\r\n"
    with pytest.raises(ct.ToolRefused, match="no file"):
        ct._apply_patch(deps, ct.ApplyPatchInput(path="nope.py", edits=[ct.Edit(old="a", new="b")]))


# ---------- run_in_sandbox ----------


def test_a_program_runs_and_its_output_is_returned(deps: ct.CodeToolDeps) -> None:
    _write(deps, "hello.py", "import sys\nprint('hello', sys.argv[1:])\n")
    out = _run(deps, "python hello.py a b")
    assert out.exit_code == 0 and "hello ['a', 'b']" in out.output
    assert out.log.startswith(".hcwma/logs/") and (deps.code_root / out.log).is_file()


def test_a_crash_is_reported_with_its_traceback(deps: ct.CodeToolDeps) -> None:
    _write(deps, "boom.py", "print(1 / 0)\n")
    out = _run(deps, "python boom.py")
    assert out.exit_code == 1 and "ZeroDivisionError" in out.output


def test_runs_in_the_chosen_folder(deps: ct.CodeToolDeps) -> None:
    _write(deps, "proj/where.py", "import os, pathlib\nprint(pathlib.Path(os.getcwd()).name)\n")
    assert "proj" in _run(deps, "python where.py", cwd="proj").output


@pytest.mark.parametrize(
    "command",
    [
        "powershell -c dir",
        "cmd /c dir",
        'python -c "print(1)"',
        "python -m pip install requests",
        "python -m http.server",
        "python notes.txt",
        "python",
        "pytest",
        "rm -rf /",
        "python ../outside.py",
    ],
)
def test_only_python_scripts_and_pytest_can_be_started(deps: ct.CodeToolDeps, command: str) -> None:
    with pytest.raises(ct.ToolRefused):
        _run(deps, command)


def test_a_never_ending_program_is_stopped(deps: ct.CodeToolDeps) -> None:
    _write(deps, "loop.py", "while True:\n    pass\n")
    out = _run(deps, "python loop.py", timeout_s=2)
    assert (
        out.stopped == "timeout" and out.exit_code is None and "stopped after" in (out.note or "")
    )
    assert out.seconds < 15


def test_endless_output_is_stopped(deps: ct.CodeToolDeps) -> None:
    _write(deps, "spam.py", "while True:\n    print('x' * 1000)\n")
    out = _run(deps, "python spam.py", timeout_s=30)
    assert out.stopped == "output_limit" and len(out.output) <= ct.MAX_OUTPUT_CHARS + 100


def test_input_is_not_available(deps: ct.CodeToolDeps) -> None:
    _write(deps, "ask.py", "print(input('name? '))\n")
    out = _run(deps, "python ask.py", timeout_s=10)
    assert out.exit_code == 1 and "EOFError" in out.output


def test_the_environment_is_scrubbed(
    deps: ct.CodeToolDeps, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("HCWMA_DATA_DIR", "C:/should/not/leak")
    _write(deps, "env.py", "import os\nprint(sorted(os.environ))\n")
    out = _run(deps, "python env.py")
    assert "OPENAI_API_KEY" not in out.output and "HCWMA_DATA_DIR" not in out.output
    assert "sk-should-not-leak" not in out.output


# ---------- the guard: escapes fail inside the sandbox ----------

ESCAPES = {
    "write_outside": "open({outside!r}, 'w').write('pwned')",
    "append_outside": "open({outside!r}, 'a').write('pwned')",
    "read_outside": "print(open({secret!r}).read())",
    "read_home_ssh": "print(open({ssh!r}).read())",
    "delete_outside": "import os; os.remove({secret!r})",
    "rename_into_outside": "import os; os.rename('mine.txt', {outside!r})",
    "list_outside": "import os; print(os.listdir({parent!r}))",
    "mkdir_outside": "import os; os.mkdir({newdir!r})",
    "rmtree_outside": "import shutil; shutil.rmtree({parent!r})",
    "network": "import socket; socket.create_connection(('127.0.0.1', 9), timeout=1)",
    "dns": "import socket; socket.getaddrinfo('example.com', 80)",
    "subprocess": "import subprocess; subprocess.run(['cmd', '/c', 'echo hi'])",
    "os_system": "import os; os.system('echo hi')",
}


@pytest.mark.parametrize("name", sorted(ESCAPES))
def test_the_sandbox_blocks_escapes(deps: ct.CodeToolDeps, layout: Layout, name: str) -> None:
    secret = layout.outside / "annek-private.txt"
    ssh = layout.home / ".ssh" / "id_ed25519"
    outside = layout.outside / "dropped.txt"
    newdir = layout.outside / "newdir"
    code = ESCAPES[name].format(
        outside=str(outside),
        secret=str(secret),
        ssh=str(ssh),
        parent=str(layout.outside),
        newdir=str(newdir),
    )
    _write(deps, "mine.txt", "mine")
    _write(deps, "escape.py", f"{code}\nprint('ESCAPED')\n")

    out = _run(deps, "python escape.py")

    assert "ESCAPED" not in out.output, out.output
    assert out.exit_code == 1 and "HCWMA sandbox" in out.output, out.output
    assert out.note and "blocked" in out.note
    assert secret.read_text(encoding="utf-8") == "not approved"
    assert not outside.exists() and not newdir.exists()
    assert (layout.outside / "annek-private.txt").exists()


def test_the_sandbox_allows_normal_work_inside_code(deps: ct.CodeToolDeps) -> None:
    program = (
        "import json, pathlib, shutil, tempfile\n"
        "data = pathlib.Path('out'); data.mkdir(exist_ok=True)\n"
        "(data / 'r.json').write_text(json.dumps({'ok': 1}))\n"
        "shutil.copy(data / 'r.json', data / 'copy.json')\n"
        "with tempfile.TemporaryDirectory() as t:\n"
        "    (pathlib.Path(t) / 'x').write_text('hi')\n"
        "print('fine', sorted(p.name for p in data.iterdir()))\n"
    )
    _write(deps, "work.py", program)
    out = _run(deps, "python work.py")
    assert out.exit_code == 0 and "fine ['copy.json', 'r.json']" in out.output, out.output


# ---------- run_tests ----------

GOOD = "def add(a, b):\n    return a + b\n"
TESTS = (
    "from calc import add\n\n"
    "def test_add():\n    assert add(2, 3) == 5\n\n"
    "def test_negative():\n    assert add(-1, 1) == 0\n"
)


def test_run_tests_counts_passes(deps: ct.CodeToolDeps) -> None:
    _write(deps, "proj/calc.py", GOOD)
    _write(deps, "proj/test_calc.py", TESTS)
    out = ct._run_tests(deps, ct.RunTestsInput(path="proj"))
    assert (out.passed, out.failed, out.errors) == (2, 0, 0), out.output
    assert out.exit_code == 0 and "2 passed" in (out.summary or "")


def test_run_tests_shows_the_failure(deps: ct.CodeToolDeps) -> None:
    _write(deps, "proj/calc.py", GOOD.replace("a + b", "a - b"))
    _write(deps, "proj/test_calc.py", TESTS)
    out = ct._run_tests(deps, ct.RunTestsInput(path="proj"))
    assert out.failed == 2 and out.passed == 0 and out.exit_code == 1
    assert "assert" in out.output and "calc.py" in out.output
    only = ct._run_tests(deps, ct.RunTestsInput(path="proj", keyword="negative"))
    assert only.failed == 1


def test_run_tests_on_a_single_file_and_with_no_tests(deps: ct.CodeToolDeps) -> None:
    _write(deps, "proj/calc.py", GOOD)
    _write(deps, "proj/test_calc.py", TESTS)
    assert ct._run_tests(deps, ct.RunTestsInput(path="proj/test_calc.py")).passed == 2
    _write(deps, "empty/readme.txt", "no tests here")
    nothing = ct._run_tests(deps, ct.RunTestsInput(path="empty"))
    assert nothing.passed is None and "no tests" in (nothing.summary or "")


def test_run_tests_does_not_read_config_above_code(deps: ct.CodeToolDeps, layout: Layout) -> None:
    (layout.repo / "pytest.ini").write_text(
        "[pytest]\naddopts = --bogus-option\n", encoding="utf-8"
    )
    _write(deps, "proj/calc.py", GOOD)
    _write(deps, "proj/test_calc.py", TESTS)
    assert ct._run_tests(deps, ct.RunTestsInput(path="proj")).passed == 2


def test_tests_that_try_to_escape_fail(deps: ct.CodeToolDeps, layout: Layout) -> None:
    target = layout.outside / "from_test.txt"
    _write(
        deps,
        "proj/test_evil.py",
        f"def test_evil():\n    open({str(target)!r}, 'w').write('x')\n",
    )
    out = ct._run_tests(deps, ct.RunTestsInput(path="proj"))
    assert out.failed == 1 and "HCWMA sandbox" in out.output and not target.exists()


# ---------- read_logs ----------


def test_read_logs_shows_the_latest_run(deps: ct.CodeToolDeps) -> None:
    with pytest.raises(ct.ToolRefused, match="no run logs"):
        ct._read_logs(deps, ct.ReadLogsInput())
    _write(deps, "p.py", "print('first run')\nraise ValueError('bad input 42')\n")
    _run(deps, "python p.py")
    logs = ct._read_logs(deps, ct.ReadLogsInput())
    assert "ValueError: bad input 42" in logs.content and len(logs.recent_logs) == 1
    only = ct._read_logs(deps, ct.ReadLogsInput(contains="valueerror"))
    assert only.content.strip().endswith("bad input 42") and "first run" not in only.content
    by_name = ct._read_logs(deps, ct.ReadLogsInput(path=logs.recent_logs[0]))
    assert by_name.log == logs.log


def test_read_logs_reads_the_tail_of_a_big_log_and_only_log_files(deps: ct.CodeToolDeps) -> None:
    big = deps.code_root / "app.log"
    big.write_text("noise\n" * 100_000 + "THE END\n", encoding="utf-8")
    out = ct._read_logs(deps, ct.ReadLogsInput(path="app.log", tail_lines=3))
    assert "THE END" in out.content
    _write(deps, "x.py", "x = 1")
    with pytest.raises(ct.ToolRefused, match="only opens"):
        ct._read_logs(deps, ct.ReadLogsInput(path="x.py"))
    with pytest.raises((ct.ToolRefused, PathDenied)):
        ct._read_logs(deps, ct.ReadLogsInput(path="../../outside/annek-private.txt"))


def test_old_logs_are_pruned(deps: ct.CodeToolDeps) -> None:
    _write(deps, "q.py", "print(1)\n")
    for _ in range(sandbox_runner.KEEP_LOGS + 4):
        sandbox_runner.run(
            "python q.py", code_root=deps.code_root, cwd=deps.code_root, timeout_s=10
        )
    logs = list((deps.code_root / ".hcwma" / "logs").glob("*.log"))
    assert len(logs) <= sandbox_runner.KEEP_LOGS
