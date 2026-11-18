"""Step 13: the static check and the manifest rules (owner rules for v1 generated tools)."""

from __future__ import annotations

import pytest

from integrations.generated_tools.manifest import ManifestError, parse_manifest
from integrations.generated_tools.static_check import check_source
from tests.integrations.generated_tools import samples


def _adapter(body: str) -> str:
    return f"def run(args, ctx):\n{body}\n"


def test_the_sample_tool_passes() -> None:
    assert check_source(samples.ADAPTER) == []
    assert check_source(samples.TESTS, is_test=True) == []
    assert check_source(samples.NETWORK_ADAPTER) == []


@pytest.mark.parametrize(
    "module",
    ["os", "subprocess", "ctypes", "socket", "shutil", "importlib", "sys", "pathlib", "io"],
)
def test_forbidden_imports_are_rejected(module: str) -> None:
    for source in (f"import {module}\n" + _adapter("    return {}"),
                   f"from {module} import x\n" + _adapter("    return {}"),
                   _adapter(f"    import {module}\n    return {{}}")):  # fmt: skip
        problems = check_source(source)
        assert any(f"'{module}'" in str(p) for p in problems), (module, problems)


@pytest.mark.parametrize(
    "body",
    [
        "    eval('1')\n    return {}",
        "    exec('x=1')\n    return {}",
        "    open('a.txt', 'w').write('x')\n    return {}",
        "    open('a.txt')\n    return {}",
        "    f = eval\n    return {}",
        "    __import__('os')\n    return {}",
        "    return {'x': getattr(args, 'y')}",
        "    return {'x': ().__class__}",
        "    return {'x': '{0.__class__}'.format(args)}",
        "    return {'x': 'a{}'.format(1)}",
        "    return {'x': globals()}",
    ],
)
def test_dangerous_calls_are_rejected(body: str) -> None:
    assert check_source(_adapter(body)), body


def test_urllib_request_and_unknown_modules_are_rejected() -> None:
    assert check_source("import urllib.request\n" + _adapter("    return {}"))
    assert check_source("from urllib import request\n" + _adapter("    return {}"))
    assert check_source("import numpy\n" + _adapter("    return {}"))
    assert not check_source("from urllib.parse import quote\n" + _adapter("    return {}"))
    assert not check_source("from urllib import parse\n" + _adapter("    return {}"))


def test_run_function_is_required_and_syntax_errors_reported() -> None:
    assert check_source("x = 1\n")
    assert check_source("def run(:\n")
    assert check_source("def run(args):\n    return {}\n")  # needs (args, ctx)


def test_re_compile_and_f_strings_are_fine() -> None:
    source = "import re\nP = re.compile(r'a+')\n" + _adapter("    return {'x': f'{len(args)}'}")
    assert check_source(source) == []


# ---- manifest ----


def _manifest(**overrides: str) -> str:
    text = samples.MANIFEST
    for old, new in overrides.items():
        text = text.replace(old.replace("__", " "), new)
    return text


def test_sample_manifest_parses_and_summarises_in_plain_words() -> None:
    manifest = parse_manifest(samples.MANIFEST)
    summary = manifest.plain_summary()
    assert "cannot read or write any file" in summary
    assert "cannot use the network" in summary


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("side_effects: none", "side_effects: writes"),
        ("side_effects: none", "side_effects: external_action"),
        ("filesystem: []", "filesystem: [C:/Users]"),
        ("secrets: []", "secrets: [api_key]"),
        ("status: quarantined", "status: approved"),
        ("created_by: tool_builder", "created_by: developer"),
        ("permissions:", "permission_tier: T4\npermissions:"),  # a manifest cannot pick its tier
        ("route: local_data", "route: custom_api"),  # custom_api needs hosts
        ("text: string", "text: bytes"),
        ("network: []", "network: [example.org]"),  # hosts only with custom_api
        ("name: word_count", "name: Word-Count"),
    ],
)
def test_manifest_cannot_ask_for_more_than_v1_allows(old: str, new: str) -> None:
    with pytest.raises(ManifestError):
        parse_manifest(samples.MANIFEST.replace(old, new, 1))


@pytest.mark.parametrize(
    "host",
    ["localhost", "127.0.0.1", "192.168.1.5", "*.example.org", "example.org:8080", "a.local"],
)
def test_network_hosts_must_be_plain_public_names(host: str) -> None:
    text = samples.NETWORK_MANIFEST.replace("[example.org]", f"[{host}]")
    with pytest.raises(ManifestError):
        parse_manifest(text)


def test_network_manifest_is_valid() -> None:
    manifest = parse_manifest(samples.NETWORK_MANIFEST)
    assert manifest.permissions.network == ["example.org"]
    assert "example.org" in manifest.plain_summary()
