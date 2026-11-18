"""Step 13 "Done when", with a fake sandbox (the real VM run is `python -m
integrations.generated_tools.proof`): a tool is generated, tested, quarantined and uncallable;
the owner approves it and it works over the real stdio MCP server; one changed line sends it
back to quarantine. Also: the Tool Builder's limits and the audit trail."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from agents.runtime.policy import PolicyLayer
from integrations.adapters.local_data.tool_builder_tools import (
    FindInput,
    NameInput,
    ScaffoldInput,
    ToolBuilderDeps,
    ToolRefused,
    _find,
    _run_tool_tests,
    _scaffold,
    _submit,
)
from integrations.generated_tools.executor import GeneratedToolError, run_generated
from integrations.generated_tools.manifest import generated_root
from integrations.generated_tools.registry import GeneratedToolSource, granted_tool_names
from integrations.generated_tools.review import ReviewRefused, ReviewService
from integrations.generated_tools.store import GeneratedStore, content_hash, read_tool
from integrations.mcp.client import McpToolClient, local_server_params
from integrations.mcp.tool_schemas import PermissionTier
from integrations.registry.capability_catalog import default_catalog
from security.audit.audit_logger import AuditLogger
from security.permissions.path_check import PathPolicy
from security.permissions.permission_manager import (
    AgentGrant,
    PermissionManager,
    load_permissions_config,
)
from tests.integrations.generated_tools import samples


@dataclass
class FakeRun:
    passed: bool = True
    destroyed: bool = True
    stopped: str | None = None
    seconds: float = 1.5
    sandbox_id: str | None = "sb-1"
    log: str = ""
    results: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.results is None:
            self.results = {
                "tests_run": 2,
                "failures": 0,
                "errors": 0,
                "passed": self.passed,
                "output": "OK",
            }


class FakeSandbox:
    def __init__(self, run: FakeRun | None = None, raises: Exception | None = None) -> None:
        self.run = run or FakeRun()
        self.raises = raises
        self.folders: list[Path] = []

    def run_tool_tests(self, folder: Path) -> FakeRun:
        self.folders.append(folder)
        if self.raises:
            raise self.raises
        return self.run


@pytest.fixture
def audit(tmp_path: Path) -> AuditLogger:
    return AuditLogger(tmp_path / "audit.jsonl")


@pytest.fixture
def sandbox() -> FakeSandbox:
    return FakeSandbox()


@pytest.fixture
def deps(audit: AuditLogger, sandbox: FakeSandbox) -> ToolBuilderDeps:
    return ToolBuilderDeps(
        audit=lambda: audit,
        sandbox=lambda: sandbox,
        reserved=lambda: frozenset({"read_file", "get_system_time"}),
        catalog_lines=lambda: [("get_system_time", "approved", "Tell the current date and time")],
    )


def _scaffold_sample(deps: ToolBuilderDeps, **over: str) -> Any:
    fields = {
        "name": "word_count",
        "manifest_yaml": samples.MANIFEST,
        "adapter_code": samples.ADAPTER,
        "test_code": samples.TESTS,
    } | over
    return _scaffold(deps, ScaffoldInput(**fields))


def _owner(audit: AuditLogger) -> ReviewService:
    return ReviewService(GeneratedStore(), audit, ["echo", "file_system"])


def _approved_tool(deps: ToolBuilderDeps, audit: AuditLogger) -> str:
    out = _scaffold_sample(deps)
    _run_tool_tests(deps, NameInput(name="word_count"))
    _submit(deps, NameInput(name="word_count"))
    _owner(audit).approve("word_count", out.hash, ["echo"])
    return str(out.hash)


def _status(name: str = "word_count") -> str | None:
    spec = default_catalog().get(name)
    return spec.manifest.status if spec else None


# ---- generate -> quarantined and uncallable ----


def test_scaffold_writes_three_files_and_starts_quarantined(
    deps: ToolBuilderDeps, audit: AuditLogger
) -> None:
    out = _scaffold_sample(deps)
    assert out.written and not out.problems and out.status == "quarantined"
    folder = generated_root() / "word_count"
    assert sorted(p.name for p in folder.iterdir()) == [
        "adapter.py",
        "manifest.yaml",
        "test_adapter.py",
    ]
    spec = default_catalog().get("word_count")
    assert spec is not None and spec.manifest.status == "quarantined"
    assert spec.manifest.permission_tier == PermissionTier.T1  # fixed by HCWMA, not the manifest
    assert [r.outcome for r in audit.read_all()] == ["written"]


def test_quarantined_tool_is_uncallable_over_real_mcp(
    deps: ToolBuilderDeps, tmp_path: Path
) -> None:
    _scaffold_sample(deps)
    agent = AgentGrant(agent_id="echo", tools=frozenset({"word_count"}), max_tier="T2")
    audit = AuditLogger(tmp_path / "a2.jsonl")

    async def go() -> tuple[Any, Any, list[str]]:
        async with McpToolClient(Client(local_server_params())) as client:
            policy = PolicyLayer(
                client=client,
                catalog=default_catalog(),
                permissions=PermissionManager(load_permissions_config()),
                audit=audit,
            )
            via_gateway = await policy.call_tool("t1", agent, "word_count", {"text": "a b"})
            direct = await client.call_tool("word_count", {"text": "a b"})  # skips the gateway
            listed = [t.name for t in await client.list_tools()]
            return via_gateway, direct, listed

    via_gateway, direct, listed = anyio.run(go)
    assert via_gateway.error_code == "denied" and "quarantined" in via_gateway.error
    assert direct.error_code == "unknown_tool"  # the tool server refuses it too
    assert "word_count" not in listed
    assert audit.read_all()[0].outcome == "denied"


def test_static_check_failure_is_written_but_never_runs_or_submits(
    deps: ToolBuilderDeps, sandbox: FakeSandbox
) -> None:
    out = _scaffold_sample(deps, adapter_code="import os\n\ndef run(args, ctx):\n    return {}\n")
    assert out.written and any("'os'" in p for p in out.problems)
    result = _run_tool_tests(deps, NameInput(name="word_count"))
    assert not result.ran and not result.passed and sandbox.folders == []
    assert not _submit(deps, NameInput(name="word_count")).submitted


def test_bad_manifest_writes_nothing(deps: ToolBuilderDeps) -> None:
    out = _scaffold_sample(deps, manifest_yaml=samples.MANIFEST.replace("none", "writes", 1))
    assert not out.written and out.problems
    assert not (generated_root() / "word_count").exists()


# ---- the Tool Builder's limits ----


def test_builder_cannot_take_a_builtin_name_or_leave_its_folder(deps: ToolBuilderDeps) -> None:
    with pytest.raises(ToolRefused, match="built-in"):
        _scaffold_sample(deps, name="read_file")
    for bad in ("../evil", "a/b", "..", "Word", "x"):
        with pytest.raises(ValueError):
            ScaffoldInput(
                name=bad,
                manifest_yaml=samples.MANIFEST,
                adapter_code=samples.ADAPTER,
                test_code=samples.TESTS,
            )


def test_path_policy_is_called_on_the_folder_before_staging(
    deps: ToolBuilderDeps, sandbox: FakeSandbox, audit: AuditLogger
) -> None:
    _scaffold_sample(deps)
    seen: list[Path] = []

    class Spy(PathPolicy):
        def check_under(self, path: Any, root: Any) -> Path:
            seen.append(Path(path))
            return super().check_under(path, root)

    spy = Spy.from_config()
    spy_deps = ToolBuilderDeps(audit=lambda: audit, sandbox=lambda: sandbox, policy=lambda: spy)
    _run_tool_tests(spy_deps, NameInput(name="word_count"))
    assert seen and seen[0] == generated_root() / "word_count"
    assert sandbox.folders  # staging happened only after the check


def test_path_policy_refuses_protected_and_outside_folders() -> None:
    policy = PathPolicy.from_config()
    root = generated_root()
    root.mkdir(parents=True, exist_ok=True)
    for bad in (Path.home() / ".ssh", root, root.parent, Path("C:/Windows")):
        with pytest.raises(PermissionError):
            policy.check_under(bad, root)
    assert policy.check_under(root / "tool", root) == (root / "tool").resolve()


def test_tests_never_run_on_the_host_when_the_sandbox_is_missing(audit: AuditLogger) -> None:
    deps = ToolBuilderDeps(
        audit=lambda: audit, sandbox=lambda: FakeSandbox(raises=RuntimeError("no wsb"))
    )
    _scaffold_sample(deps)
    result = _run_tool_tests(deps, NameInput(name="word_count"))
    assert not result.ran and not result.passed
    assert "never run outside the sandbox" in result.next_step
    assert audit.read_all()[-1].outcome == "sandbox_unavailable"


def test_every_sandbox_run_writes_an_audit_record(
    deps: ToolBuilderDeps, audit: AuditLogger
) -> None:
    _scaffold_sample(deps)
    _run_tool_tests(deps, NameInput(name="word_count"))
    record = audit.read_all()[-1]
    assert (record.event, record.tool, record.outcome) == ("generated_tool", "word_count", "passed")
    assert record.args["action"] == "sandbox_test" and record.args["destroyed"] is True


def test_find_existing_tools(deps: ToolBuilderDeps) -> None:
    assert _find(deps, FindInput(query="current time")).tools[0].name == "get_system_time"
    assert _find(deps, FindInput(query="zebra")).count == 0


# ---- test -> submit -> approve ----


def test_submit_needs_a_passing_sandbox_run_for_these_exact_files(
    deps: ToolBuilderDeps, sandbox: FakeSandbox
) -> None:
    _scaffold_sample(deps)
    assert "have not passed" in _submit(deps, NameInput(name="word_count")).reason
    sandbox.run = FakeRun(passed=False)
    assert not _run_tool_tests(deps, NameInput(name="word_count")).passed
    assert not _submit(deps, NameInput(name="word_count")).submitted
    sandbox.run = FakeRun(destroyed=False)  # a VM that could not be confirmed gone is a fail
    assert not _run_tool_tests(deps, NameInput(name="word_count")).passed


def test_owner_approval_rules(deps: ToolBuilderDeps, audit: AuditLogger) -> None:
    out = _scaffold_sample(deps)
    owner = _owner(audit)
    with pytest.raises(ReviewRefused, match="tests have not passed"):
        owner.approve("word_count", out.hash, ["echo"])
    _run_tool_tests(deps, NameInput(name="word_count"))
    with pytest.raises(ReviewRefused, match="not submitted"):
        owner.approve("word_count", out.hash, ["echo"])
    _submit(deps, NameInput(name="word_count"))
    with pytest.raises(ReviewRefused, match="changed since you looked"):
        owner.approve("word_count", "0" * 64, ["echo"])
    with pytest.raises(ReviewRefused, match="at least one existing agent"):
        owner.approve("word_count", out.hash, [])
    with pytest.raises(ReviewRefused, match="unknown: nobody"):
        owner.approve("word_count", out.hash, ["nobody"])
    assert owner.approve("word_count", out.hash, ["echo"])["status"] == "approved"
    assert audit.read_all()[-1].args["action"] == "approve"


def test_review_shows_summary_permissions_and_full_code(
    deps: ToolBuilderDeps, audit: AuditLogger
) -> None:
    _scaffold_sample(deps)
    review = _owner(audit).review("word_count")
    assert "Count the words" in review["summary"]
    assert review["permissions"]["network"] == [] and review["permissions"]["filesystem"] == []
    assert review["adapter_code"] == samples.ADAPTER and review["test_code"] == samples.TESTS
    assert review["manifest_yaml"] == samples.MANIFEST


# ---- approved -> callable; edited -> quarantined ----


def test_approved_tool_is_callable_then_one_changed_line_quarantines_it(
    deps: ToolBuilderDeps, tmp_path: Path, audit: AuditLogger
) -> None:
    _approved_tool(deps, audit)
    call_audit = AuditLogger(tmp_path / "calls.jsonl")
    # the owner assigned the tool to echo: the grant comes from the owner-only state file
    assert granted_tool_names("echo") == {"word_count"}
    assert granted_tool_names("file_system") == frozenset()

    async def call(agent_id: str, text: str) -> Any:
        grant = AgentGrant(
            agent_id=agent_id, tools=granted_tool_names(agent_id), max_tier=PermissionTier.T2
        )
        async with McpToolClient(Client(local_server_params())) as client:
            policy = PolicyLayer(
                client=client,
                catalog=default_catalog(),
                permissions=PermissionManager(load_permissions_config()),
                audit=call_audit,
            )
            return await policy.call_tool("task-x", grant, "word_count", {"text": text})

    ok = anyio.run(call, "echo", "one two  three\nfour")
    assert ok.ok, ok.error
    assert ok.data == {"words": 4, "lines": 2, "characters": 19}
    # an agent the owner did not assign it to cannot call it
    assert anyio.run(call, "file_system", "x").error_code == "denied"
    # audited like any other tool call
    assert [(r.tool, r.agent, r.outcome) for r in call_audit.read_all()] == [
        ("word_count", "echo", "ok"),
        ("word_count", "file_system", "denied"),
    ]

    # edit ONE line of the code
    adapter = generated_root() / "word_count" / "adapter.py"
    adapter.write_text(
        adapter.read_text().replace("len(text)}", "len(text) + 1}"), encoding="utf-8"
    )
    assert _status() == "quarantined"
    assert granted_tool_names("echo") == frozenset()
    again = anyio.run(call, "echo", "a b")
    assert again.error_code == "denied"
    assert "changed since the owner approved" in _owner(audit).review("word_count")["reason"]


@pytest.mark.parametrize("filename", ["manifest.yaml", "test_adapter.py"])
def test_any_changed_file_quarantines(
    deps: ToolBuilderDeps, audit: AuditLogger, filename: str
) -> None:
    _approved_tool(deps, audit)
    target = generated_root() / "word_count" / filename
    target.write_text(target.read_text() + "\n# x\n", encoding="utf-8")
    assert _status() == "quarantined"


def test_extra_file_and_manifest_permission_change_quarantine(
    deps: ToolBuilderDeps, audit: AuditLogger
) -> None:
    _approved_tool(deps, audit)
    folder = generated_root() / "word_count"
    (folder / "helper.py").write_text("x = 1\n", encoding="utf-8")
    assert _status() == "quarantined"
    (folder / "helper.py").unlink()
    assert _status() == "approved"
    manifest = folder / "manifest.yaml"
    manifest.write_text(manifest.read_text().replace("network: []", "network: [example.org]"))
    tool = read_tool("word_count")
    assert tool is not None and tool.manifest is None  # invalid now (hosts need custom_api)
    assert default_catalog().get("word_count") is None


def test_disable_and_rollback(deps: ToolBuilderDeps, audit: AuditLogger) -> None:
    first = _approved_tool(deps, audit)
    owner = _owner(audit)
    assert owner.disable("word_count")["status"] == "disabled"
    assert _status() == "disabled"
    assert owner.enable("word_count")["status"] == "approved"
    # a new version: back to quarantine; the owner can roll back to the approved one
    adapter = generated_root() / "word_count" / "adapter.py"
    adapter.write_text(adapter.read_text().replace("lines =", "lines = 0 * 1 +"), encoding="utf-8")
    assert _status() == "quarantined"
    with pytest.raises(ReviewRefused, match="never approved"):
        owner.rollback("word_count", "f" * 64)
    assert owner.rollback("word_count", first)["status"] == "approved"
    assert adapter.read_text() == samples.ADAPTER


def test_the_tool_cannot_approve_itself_through_its_manifest(deps: ToolBuilderDeps) -> None:
    out = _scaffold_sample(deps, manifest_yaml=samples.MANIFEST.replace("quarantined", "approved"))
    assert not out.written and "status" in out.problems[0]


# ---- running an approved tool ----


def _files(adapter: str, tests: str = samples.TESTS) -> tuple[dict[str, bytes], str]:
    """The three files as bytes and their hash, as the owner would have approved them."""
    files = {
        "manifest.yaml": samples.MANIFEST.encode(),
        "adapter.py": adapter.encode(),
        "test_adapter.py": tests.encode(),
    }
    return files, content_hash(files)


def _run(adapter: str, args: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    files, digest = _files(adapter)
    return run_generated(files, digest, args or {}, **kw)


def test_runner_rechecks_the_approved_hash_before_exec() -> None:
    files, digest = _files(samples.ADAPTER)
    assert run_generated(files, digest, {"text": "a b"})["words"] == 2
    # one byte differs from what was approved: nothing is executed
    changed = {**files, "adapter.py": files["adapter.py"] + b"\n# x\n"}
    with pytest.raises(GeneratedToolError, match="do not match the version the owner approved"):
        run_generated(changed, digest, {"text": "a b"})
    for name in files:  # any one of the three files counts
        other = {**files, name: files[name] + b" "}
        with pytest.raises(GeneratedToolError, match="do not match"):
            run_generated(other, digest, {"text": "a"})


def test_runner_reruns_the_static_check_before_exec() -> None:
    # Even with a matching hash (as if it had been approved), forbidden code does not run.
    bad = "import os\n\ndef run(args, ctx):\n    return {}\n"
    with pytest.raises(GeneratedToolError, match="static check failed again"):
        _run(bad)


def test_file_changed_after_approval_is_refused_at_call_time(
    deps: ToolBuilderDeps, audit: AuditLogger
) -> None:
    _approved_tool(deps, audit)
    spec = default_catalog().get("word_count")  # looked up while still approved
    assert spec is not None and spec.manifest.status == "approved"
    assert spec.handler(spec.input_model(text="a b c")).words == 3
    adapter = generated_root() / "word_count" / "adapter.py"
    adapter.write_text(adapter.read_text() + "\n# changed after approval\n", encoding="utf-8")
    with pytest.raises(PermissionError, match="changed since the owner approved"):
        spec.handler(spec.input_model(text="a b c"))  # same stale spec: still refused


def test_restricted_builtins_and_imports_in_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    # the runner imports `static_check` as a sibling file, as it does in the child process
    monkeypatch.syspath_prepend(str(Path(run_generated.__code__.co_filename).parent))
    import importlib

    runner = importlib.import_module("integrations.generated_tools.runner")
    for name in ("os", "subprocess", "sys", "urllib.request", "importlib"):
        with pytest.raises(ImportError):
            runner._guarded_import(name)
    assert runner._guarded_import("re").__name__ == "re"
    assert "open" not in runner.SAFE_NAMES and "eval" not in runner.SAFE_NAMES


def test_runtime_has_a_time_limit_and_returns_only_a_dict() -> None:
    with pytest.raises(GeneratedToolError, match="did not finish"):
        _run("def run(args, ctx):\n    while True:\n        pass\n", timeout_s=1.5)
    with pytest.raises(GeneratedToolError, match="must return a dict"):
        _run("def run(args, ctx):\n    return 5\n", timeout_s=10)


def test_network_goes_only_through_declared_hosts() -> None:
    from integrations.adapters.browser.web_tools import FetchedPage, ToolRefused, fetch_public

    calls: list[tuple[str, frozenset[str]]] = []

    def fake(url: str, hosts: frozenset[str]) -> FetchedPage:
        calls.append((url, hosts))
        return FetchedPage(url, url, 200, "text/html", "<h1>Hi</h1>")

    hosts = frozenset({"example.org"})
    files, digest = _files(samples.NETWORK_ADAPTER)
    out = run_generated(files, digest, {"path": "x"}, hosts=hosts, fetch=fake)
    assert out == {"title": "Hi"} and calls == [("https://example.org/x", hosts)]
    # a tool with no declared hosts cannot fetch at all
    with pytest.raises(GeneratedToolError, match="declared no network hosts"):
        run_generated(files, digest, {"path": "x"}, hosts=frozenset(), fetch=fake)
    # the real fetch refuses any other host before touching the network
    with pytest.raises(ToolRefused, match="not a host this tool declared"):
        fetch_public("https://evil.example.net/", allowed_hosts=hosts)
    # private addresses stay refused even for a declared host (shared check_url)
    with pytest.raises(ToolRefused, match="not a public internet address|could not be found"):
        fetch_public("http://127.0.0.1/", allowed_hosts=frozenset({"127.0.0.1"}))


def test_output_must_match_the_declared_outputs(deps: ToolBuilderDeps, audit: AuditLogger) -> None:
    bad = samples.ADAPTER.replace('"characters": len(text)', '"extra": 1')
    _scaffold_sample(deps, adapter_code=bad)
    _run_tool_tests(deps, NameInput(name="word_count"))
    _submit(deps, NameInput(name="word_count"))
    tool = read_tool("word_count")
    assert tool is not None
    _owner(audit).approve("word_count", tool.digest, ["echo"])
    spec = GeneratedToolSource(reserved=frozenset()).get("word_count")
    assert spec is not None
    with pytest.raises(ValueError):
        spec.handler(spec.input_model(text="a"))


# ---- who may call an approved tool: assigned at approval AND allowed by the policy layer ----


def test_protected_path_covers_the_generated_tools_state_folder() -> None:
    policy = PathPolicy.from_config()
    state = Path.home() / "AppData" / "Local" / "HCWMA" / "generated_tools"
    for target in (state, state / "state.json", state / "snapshots" / "x" / "adapter.py"):
        for access in ("read", "write", "modify"):
            with pytest.raises(PermissionError, match="protected"):
                policy.check(target, access)  # type: ignore[arg-type]


def test_callable_only_if_assigned_at_approval_and_allowed_by_policy(
    deps: ToolBuilderDeps, audit: AuditLogger
) -> None:
    from agents.registry.agent_registry import AgentRegistry
    from agents.runtime.agent_runtime import _grant_for

    _approved_tool(deps, audit)  # approved for "echo" only
    registry = AgentRegistry.from_dir()
    manager = PermissionManager(load_permissions_config())
    spec = default_catalog().get("word_count")
    assert spec is not None and spec.manifest.status == "approved"

    # 1. assigned (echo) but the card's tier is too low: the policy layer refuses
    echo = registry.get("echo")
    assert echo is not None and echo.permission_tier == PermissionTier.T0
    granted = _grant_for(echo, None, read_only=False)
    assert "word_count" in granted.tools  # the assignment is there ...
    decision = manager.check(granted, spec.manifest)
    assert (
        decision.outcome == "denied" and "above agent max tier" in decision.reason
    )  # ... not enough

    # 2. allowed by policy (tier ok) but NOT assigned: refused on the allowlist
    other = registry.get("web_research")
    assert other is not None and other.permission_tier >= PermissionTier.T1
    not_assigned = _grant_for(other, None, read_only=False)
    assert "word_count" not in not_assigned.tools
    decision = manager.check(not_assigned, spec.manifest)
    assert decision.outcome == "denied" and "allowlist" in decision.reason

    # 3. both: assign the tool to an agent whose tier allows T1 -> allowed
    tool = read_tool("word_count")
    assert tool is not None
    ReviewService(GeneratedStore(), audit, ["echo", "web_research"]).approve(
        "word_count", tool.digest, ["web_research"]
    )
    both = _grant_for(other, None, read_only=False)
    assert "word_count" in both.tools
    assert manager.check(both, spec.manifest).outcome == "allowed"
    # and a read-only delegation never raises privilege (still T1 here)
    assert manager.check(_grant_for(other, None, read_only=True), spec.manifest).allowed
