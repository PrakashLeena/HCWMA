"""Shared path check for every filesystem tool (architecture rule 10, master document 9.4).

`PathPolicy.check(path, access)` resolves the real path (following symlinks and `..`) and
then applies, in order:

1. protected paths — always refused, even inside an approved folder;
2. owner-blocked folders — refused;
3. `read`: must be inside an approved folder or the HCWMA workspace;
   `write`: must be inside the workspace (writes elsewhere are T3, approved per action).

Relative tool paths are taken relative to the workspace. Comparisons are case-insensitive
on Windows.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from security.permissions.config import REPO_ROOT, PermissionsConfig, load_permissions_config

Access = Literal["read", "write"]


class PathDenied(PermissionError):
    """Raised when a path is outside what an agent tool may touch."""


def _key(path: Path) -> Path:
    return Path(os.path.normcase(str(path)))


def _inside(child: Path, parent: Path) -> bool:
    return _key(child).is_relative_to(_key(parent))


class PathPolicy:
    def __init__(
        self,
        *,
        repo_root: Path,
        home: Path,
        workspace: str,
        approved_folders: Iterable[str],
        protected_paths: Iterable[str],
        blocked_folders: Iterable[str],
    ) -> None:
        self._repo_root = repo_root.resolve()
        self._home = home
        self.workspace = self._expand(workspace)
        self.approved = [self._expand(p) for p in approved_folders]
        self.protected = [self._expand(p) for p in protected_paths]
        self.blocked = [self._expand(p) for p in blocked_folders]

    @classmethod
    def from_config(cls, config: PermissionsConfig | None = None) -> PathPolicy:
        cfg = config or load_permissions_config()
        return cls(
            repo_root=REPO_ROOT,
            home=Path.home(),
            workspace=cfg.workspace,
            approved_folders=cfg.approved_folders,
            protected_paths=cfg.protected_paths,
            blocked_folders=cfg.blocked_folders,
        )

    def _expand(self, entry: str) -> Path:
        if entry == "~" or entry.startswith(("~/", "~\\")):
            path = self._home / entry[2:]
        else:
            path = Path(entry)
            if not path.is_absolute():
                path = self._repo_root / path
        return path.resolve()

    def is_protected(self, path: Path) -> bool:
        """True if `path` is, or is inside, a protected path. Indexers use this to prune."""
        real = Path(path).resolve()
        return any(_inside(real, p) for p in self.protected)

    def check(self, path: str | Path, access: Access) -> Path:
        """Return the resolved path if allowed, else raise `PathDenied`."""
        raw = Path(path)
        if not raw.is_absolute():
            raw = self.workspace / raw
        real = raw.resolve()

        if self.is_protected(real):
            raise PathDenied(f"'{path}' is under a protected path")
        if any(_inside(real, b) for b in self.blocked):
            raise PathDenied(f"'{path}' is in a folder blocked by the owner")
        in_workspace = _inside(real, self.workspace)
        if access == "write":
            if not in_workspace:
                raise PathDenied(f"'{path}' is outside the HCWMA workspace")
            return real
        if in_workspace or any(_inside(real, a) for a in self.approved):
            return real
        raise PathDenied(f"'{path}' is not inside an approved folder")
