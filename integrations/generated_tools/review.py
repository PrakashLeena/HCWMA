"""The owner's review of generated tools (build step 13): list, show everything, approve,
disable, enable, roll back.

Only the token-protected backend (`backend/api/app.py`) reaches this. No tool an agent can
call does, and the Tool Builder's tools only record facts about the exact files they handled.

Approving is bound to a hash: the owner approves the files they were shown, and the call is
refused if a single byte changed since (so code cannot be swapped in between "look" and
"approve"). It also needs a passed sandbox test and a submission for the same hash.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from integrations.generated_tools.manifest import ADAPTER_FILE, MANIFEST_FILE, TEST_FILE
from integrations.generated_tools.store import (
    GeneratedStore,
    ToolFiles,
    content_hash,
    list_tool_names,
    read_tool,
)
from security.audit.audit_logger import AuditLogger, get_audit_logger


class ReviewRefused(ValueError):
    """The owner's request cannot be done. The message is shown to the owner."""


@dataclass
class ReviewService:
    store: GeneratedStore
    audit: AuditLogger
    known_agents: list[str]

    @classmethod
    def default(cls, known_agents: list[str] | None = None) -> ReviewService:
        agents = known_agents
        if agents is None:
            from agents.registry.agent_registry import AgentRegistry

            agents = sorted(c.id for c in AgentRegistry.from_dir().workers())
        return cls(store=GeneratedStore(), audit=get_audit_logger(), known_agents=agents)

    # -- reading -----------------------------------------------------------------------------

    def _tool(self, name: str) -> ToolFiles:
        tool = read_tool(name, self.store.root)
        if tool is None:
            raise ReviewRefused(f"there is no generated tool named '{name}'")
        return tool

    def list_tools(self) -> list[dict[str, Any]]:
        rows = []
        for name in list_tool_names(self.store.root):
            tool = read_tool(name, self.store.root)
            if tool is None:
                continue
            status, reason = self.store.status_of(tool)
            state = self.store.state(name)
            rows.append(
                {
                    "name": name,
                    "status": status,
                    "reason": reason,
                    "description": tool.manifest.description if tool.manifest else "",
                    "version": tool.manifest.version if tool.manifest else "",
                    "tested": bool(
                        state.tested
                        and state.tested.get("hash") == tool.digest
                        and state.tested.get("passed")
                    ),
                    "submitted": bool(
                        state.submitted and state.submitted.get("hash") == tool.digest
                    ),
                }
            )
        return rows

    def review(self, name: str) -> dict[str, Any]:
        """Everything the owner needs to decide: the plain summary, the permissions as
        declared AND the full code. The summary is written by code from the manifest."""
        tool = self._tool(name)
        status, reason = self.store.status_of(tool)
        state = self.store.state(name)
        manifest = tool.manifest
        return {
            "name": name,
            "status": status,
            "reason": reason,
            "hash": tool.digest,
            "summary": manifest.plain_summary() if manifest else "(the manifest cannot be read)",
            "permissions": (
                {
                    "tier": "T1 (read-only, fixed by HCWMA)",
                    "side_effects": manifest.side_effects,
                    "network": list(manifest.permissions.network),
                    "filesystem": [],
                    "secrets": [],
                    "approval_required_per_call": manifest.approval_required,
                    "timeout_s": manifest.timeout_s,
                }
                if manifest
                else {}
            ),
            "problems": list(tool.problems),
            "manifest_yaml": tool.text(MANIFEST_FILE),
            "adapter_code": tool.text(ADAPTER_FILE),
            "test_code": tool.text(TEST_FILE),
            "tested": _tested(state.tested, tool.digest),
            "submitted": bool(state.submitted and state.submitted.get("hash") == tool.digest),
            "agents_granted": self.store.granted_agents(tool),
            "agents_available": self.known_agents,
            "previous_versions": [
                {"hash": h["hash"], "version": h.get("version"), "at": h.get("at")}
                for h in state.approved_history
                if h.get("hash") != tool.digest
            ],
        }

    # -- owner decisions ---------------------------------------------------------------------

    def approve(self, name: str, digest: str, agents: list[str]) -> dict[str, Any]:
        tool = self._tool(name)
        if tool.digest != digest:
            raise ReviewRefused("the files changed since you looked at them; review them again")
        if not tool.ok:
            raise ReviewRefused(
                "the tool does not pass the checks: " + "; ".join(tool.problems[:3])
            )
        state = self.store.state(name)
        if not state.tested or state.tested.get("hash") != digest or not state.tested.get("passed"):
            raise ReviewRefused("the sandbox tests have not passed for these exact files")
        if not state.submitted or state.submitted.get("hash") != digest:
            raise ReviewRefused("the Tool Builder has not submitted these exact files for review")
        unknown = sorted(set(agents) - set(self.known_agents))
        if not agents or unknown:
            raise ReviewRefused(
                "choose at least one existing agent that may use the tool"
                + (f" (unknown: {', '.join(unknown)})" if unknown else "")
            )
        self.store.approve(tool, agents)
        self.audit.generated_tool(
            action="approve", tool=name, agent="owner", outcome="approved",
            details={"hash": digest[:16], "agents": ",".join(sorted(agents))},
        )  # fmt: skip
        return self.review(name)

    def disable(self, name: str) -> dict[str, Any]:
        self._tool(name)
        self.store.set_disabled(name, True)
        self.audit.generated_tool(action="disable", tool=name, agent="owner", outcome="disabled")
        return self.review(name)

    def enable(self, name: str) -> dict[str, Any]:
        """Switch a disabled tool back on. It is approved again only if its files still match
        an approval; otherwise it stays quarantined."""
        self._tool(name)
        self.store.set_disabled(name, False)
        self.audit.generated_tool(action="enable", tool=name, agent="owner", outcome="enabled")
        return self.review(name)

    def rollback(self, name: str, digest: str) -> dict[str, Any]:
        """Put an earlier APPROVED version back (the owner approved exactly those files
        before). The current files are not deleted silently: they are replaced, and the
        previous approval of the restored version applies again."""
        state = self.store.state(name)
        record = next((h for h in state.approved_history if h.get("hash") == digest), None)
        if record is None:
            raise ReviewRefused("that version was never approved, so it cannot be rolled back to")
        files = self.store.snapshot_files(name, digest)
        if files is None or content_hash(files) != digest:
            raise ReviewRefused("the saved copy of that version is missing or damaged")
        self.store.restore_files(name, files)
        restored = self._tool(name)
        if restored.digest != digest:
            raise ReviewRefused("the restored files do not match the saved version")
        self.store._update(  # noqa: SLF001 - the owner's own rollback re-applies that approval
            name, approved=record, disabled=None
        )
        self.audit.generated_tool(
            action="rollback", tool=name, agent="owner", outcome="rolled_back",
            details={"hash": digest[:16]},
        )  # fmt: skip
        return self.review(name)


def _tested(tested: dict[str, Any] | None, digest: str) -> dict[str, Any] | None:
    if tested and tested.get("hash") == digest:
        return {
            "passed": bool(tested.get("passed")),
            "at": tested.get("at"),
            "summary": tested.get("summary"),
        }
    return None
