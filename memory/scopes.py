"""Memory scopes (master document 8, `docs/memory-policy.md` section 2).

An agent's card lists the scopes it may read. The memory service refuses every other scope.
"""

from __future__ import annotations

from typing import Literal

Scope = Literal["profile", "episodic", "summaries", "skill_notes"]
Source = Literal["owner_said", "tool_result", "summary"]

# Long-term layers kept in the memory database.
PROFILE = "profile"  # what the owner stated about themselves and how they want HCWMA to act
EPISODIC = "episodic"  # outcomes of finished tasks
SUMMARIES = "summaries"  # one short summary per chat session
SKILL_NOTES = "skill_notes"  # lessons that belong to one agent
STORED_SCOPES: tuple[str, ...] = (PROFILE, EPISODIC, SUMMARIES, SKILL_NOTES)

# Scopes a card may also name. They are not rows in the memory database:
# - pc_index: the PC file index (memory/pc_index), reached through the file tools;
# - state:    structured state owned by the tool that writes it (none yet);
# - task:     the running task's own working notes (the agent's message list), which is
#             dropped when the task ends and never stored.
OTHER_SCOPES: tuple[str, ...] = ("pc_index", "state", "task")
KNOWN_SCOPES: frozenset[str] = frozenset(STORED_SCOPES + OTHER_SCOPES)

# Only what the owner said is fully trusted; everything else is labelled lower.
TRUST: dict[str, str] = {"owner_said": "high", "tool_result": "low", "summary": "low"}
SOURCE_LABEL: dict[str, str] = {
    "owner_said": "owner said",
    "tool_result": "from a tool",
    "summary": "summary",
}
