"""Step 6: the shared path check (architecture rule 10, master document 9.4)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from security.permissions.path_check import PathDenied, PathPolicy


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    approved = tmp_path / "docs"
    for p in (repo / "workspace", repo / "security", home / ".ssh", approved / "sub"):
        p.mkdir(parents=True)
    (approved / "sub" / "a.txt").write_text("x", encoding="utf-8")
    (home / ".ssh" / "id_ed25519").write_text("secret", encoding="utf-8")
    return {"repo": repo, "home": home, "approved": approved, "tmp": tmp_path}


def _policy(layout: dict[str, Path], approved: list[str] | None = None) -> PathPolicy:
    return PathPolicy(
        repo_root=layout["repo"],
        home=layout["home"],
        workspace="workspace",
        approved_folders=approved if approved is not None else [str(layout["approved"])],
        protected_paths=["~/.ssh", "security/"],
        blocked_folders=[],
    )


def test_read_inside_approved_folder_is_allowed(layout: dict[str, Path]) -> None:
    target = layout["approved"] / "sub" / "a.txt"
    assert _policy(layout).check(target, "read") == target.resolve()


def test_read_inside_workspace_is_allowed(layout: dict[str, Path]) -> None:
    target = layout["repo"] / "workspace" / "new.txt"
    assert _policy(layout).check(target, "read") == target.resolve()


def test_read_outside_approved_folders_is_refused(layout: dict[str, Path]) -> None:
    with pytest.raises(PathDenied, match="not inside an approved folder"):
        _policy(layout).check(layout["tmp"] / "elsewhere.txt", "read")


def test_nothing_is_readable_when_no_folder_is_approved(layout: dict[str, Path]) -> None:
    with pytest.raises(PathDenied):
        _policy(layout, approved=[]).check(layout["approved"] / "sub" / "a.txt", "read")


def test_protected_path_is_refused_even_if_approved(layout: dict[str, Path]) -> None:
    policy = _policy(layout, approved=[str(layout["home"])])
    with pytest.raises(PathDenied, match="protected"):
        policy.check(layout["home"] / ".ssh" / "id_ed25519", "read")
    with pytest.raises(PathDenied, match="protected"):
        policy.check(layout["repo"] / "security" / "x", "read")


def test_approving_a_parent_of_a_protected_path_does_not_expose_it(
    layout: dict[str, Path],
) -> None:
    policy = _policy(layout, approved=[str(layout["tmp"])])
    with pytest.raises(PathDenied, match="protected"):
        policy.check(layout["home"] / ".ssh", "read")


def test_dotdot_escape_is_refused(layout: dict[str, Path]) -> None:
    sneaky = layout["approved"] / ".." / "home" / ".ssh" / "id_ed25519"
    with pytest.raises(PathDenied):
        _policy(layout).check(sneaky, "read")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlink support")
def test_symlink_escape_is_refused(layout: dict[str, Path]) -> None:
    link = layout["approved"] / "link"
    try:
        link.symlink_to(layout["home"] / ".ssh", target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks needs Developer Mode or admin on Windows")
    with pytest.raises(PathDenied, match="protected"):
        _policy(layout).check(link / "id_ed25519", "read")


def test_write_only_inside_workspace(layout: dict[str, Path]) -> None:
    policy = _policy(layout)
    ok = layout["repo"] / "workspace" / "out.txt"
    assert policy.check(ok, "write") == ok.resolve()
    with pytest.raises(PathDenied, match="workspace"):
        policy.check(layout["approved"] / "sub" / "a.txt", "write")


def test_blocked_folder_is_refused(layout: dict[str, Path]) -> None:
    policy = PathPolicy(
        repo_root=layout["repo"],
        home=layout["home"],
        workspace="workspace",
        approved_folders=[str(layout["approved"])],
        protected_paths=[],
        blocked_folders=[str(layout["approved"] / "sub")],
    )
    with pytest.raises(PathDenied, match="blocked"):
        policy.check(layout["approved"] / "sub" / "a.txt", "read")


def test_real_config_protects_ssh_and_security_folder() -> None:
    policy = PathPolicy.from_config()
    with pytest.raises(PathDenied):
        policy.check(Path.home() / ".ssh" / "id_rsa", "read")
    with pytest.raises(PathDenied):
        policy.check(Path(__file__).resolve().parents[3] / "security" / "audit", "read")
