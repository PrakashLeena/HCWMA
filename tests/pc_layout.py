"""A throw-away "PC" in a temp folder for Step 8 tests: one approved folder with a protected
sub-folder, a workspace, a home with `.ssh`, and a folder that is not approved at all.
Tests never touch the owner's real files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from memory.pc_index.index import FileIndex
from security.permissions.path_check import PathPolicy


@dataclass
class Layout:
    tmp: Path
    repo: Path
    home: Path
    docs: Path  # approved
    workspace: Path
    secrets: Path  # protected, inside the approved folder
    outside: Path  # not approved
    policy: PathPolicy
    index: FileIndex


def write(path: Path, text: str = "x", *, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def make_layout(tmp_path: Path) -> Layout:
    tmp = tmp_path.resolve()
    repo, home, docs, outside = tmp / "repo", tmp / "home", tmp / "docs", tmp / "outside"
    workspace, secrets = repo / "workspace", docs / "secrets"
    for folder in (workspace, repo / "security", home / ".ssh", docs, secrets, outside):
        folder.mkdir(parents=True)

    write(docs / "projects" / "annek-proposal_v2.docx", "proposal")
    write(docs / "projects" / "budget 2026.xlsx", "budget!!")
    write(docs / "notes" / "meeting notes.txt", "notes")
    write(docs / "node_modules" / "pkg" / "index.js", "noise")
    write(secrets / "annek-passwords.txt", "hunter2")
    write(home / ".ssh" / "id_ed25519", "private key")
    write(outside / "annek-private.txt", "not approved")
    write(workspace / "draft-report.md", "draft")

    policy = PathPolicy(
        repo_root=repo,
        home=home,
        workspace="workspace",
        approved_folders=[str(docs)],
        protected_paths=["~/.ssh", "security/", str(secrets)],
        blocked_folders=[],
    )
    return Layout(
        tmp=tmp,
        repo=repo,
        home=home,
        docs=docs,
        workspace=workspace,
        secrets=secrets,
        outside=outside,
        policy=policy,
        index=FileIndex(tmp / "index" / "index.sqlite"),
    )
