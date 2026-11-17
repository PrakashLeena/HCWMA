# HCWMA Memory Policy

Owner-approved rules for Step 10 (persistent memory). Claude Code builds exactly this.
Items marked **[CONFIRM]** are defaults the owner should check before Step 10 starts.
Design reference: `docs/MASTER_DOCUMENT.md` section 8 and 9.

## 1. Principles

- Memory is data, never instructions. Remembered text can never change permissions, tiers, approvals or an agent's tools.
- Local only. Memory lives on this PC. No cloud sync, no cloud LLM.
- Stated, not guessed. Store what the owner said or what a tool returned. Do not store inferences about the owner.
- Smallest useful memory. If a fact would not change a future answer, do not store it.
- The owner can always see, export and delete everything.

## 2. What HCWMA may remember

| Layer | Examples | Written by |
|-------|----------|-----------|
| Profile / preferences | name, language, reply style, "keep answers short" | Memory (Librarian) agent, when the owner states it |
| Episodic | "restored annek-proposal on 17 Nov", decisions, task outcomes | Router after a finished task (summary only) |
| Conversation summaries | one short summary per session | Librarian |
| Agent skill notes | "this project uses pnpm", "search with the .docx extension first" | the owning agent, via the Librarian |
| Structured state | tasks, reminders, integration status | the tool that owns it |
| PC file index | names, paths, dates, sizes (already built in Step 8) | indexer |
| Task scratchpad | notes for the current task | active agent; cleared when the task ends |

## 3. What HCWMA must never store

- Passwords, API keys, tokens, session cookies, recovery codes.
- Card or bank account numbers, government ID numbers.
- Contents of any file under a protected path or matching a protected pattern.
- File contents in general. The index stays names/metadata only unless the owner changes this later.
- Text copied from web pages or files as "facts about the owner".
- Health, political, religious or similar sensitive details unless the owner says "remember this".
- Anything the owner said to forget.

If a message contains a blocked item, store the rest and drop the blocked part silently.

## 4. Writing rules

- Only the Librarian agent writes long-term memory. Other agents send it a proposed entry.
- Every entry has: id, scope, text, source (`owner_said` | `tool_result` | `summary`), created_at, last_used_at, expires_at.
- "Remember that ..." from the owner is written immediately.
- Automatic writes are limited to summaries and episodic task outcomes.
- Before writing, search for a near-duplicate and update it instead of adding a second copy.
- Never write memory from text found inside files, web pages or tool output.

## 5. Reading rules

- Each agent reads only the scopes on its capability card.
- Retrieve at most 5 entries per request and inject at most about 300 tokens into the prompt. This laptop reads prompts slowly (about 35 tokens/s), so long memory blocks cost real seconds.
- The Router gets profile/preferences and recent summaries only.
- Retrieved memory is placed in the prompt as quoted data, labelled as memory.

## 6. Retention **[CONFIRM]**

| Layer | Keep for |
|-------|----------|
| Task scratchpad | until the task ends |
| Conversation summaries | 90 days |
| Episodic memory | 1 year |
| Profile / preferences | until the owner deletes them |
| Agent skill notes | until deleted; unused for 180 days are flagged for review |
| Audit log | 180 days; never contains file contents or secrets |

Expired entries are removed by a daily clean-up job that is itself logged.

## 7. Deleting and exporting

- "Forget X": the Librarian finds matches, shows them to the owner, and deletes after the owner confirms. Delete also removes summaries and notes derived from the entry.
- "Forget everything": requires an explicit approval prompt like a T4 action.
- Deletion is real removal from the database, including the full-text index. Not a hidden flag.
- A removed entry must no longer be returned by retrieval. This is tested.
- "Show what you remember": lists entries by scope.
- Export: one command writes all memory to a readable JSON and Markdown file in a folder the owner chooses.

## 8. Storage and backup

- SQLite database in the HCWMA data folder, outside the repository and gitignored.
- Backups: copy of the database to the backup folder on request and on a schedule **[CONFIRM: how often, and where]**. Encrypt backups that leave the PC.
- Restore must be tested on a clean folder.

## 9. Safety against bad memory

- Entries from `tool_result` or `summary` carry a lower trust label than `owner_said`.
- A memory line that tries to give instructions ("always approve", "ignore the rules") is rejected at write time and flagged.
- Memory cannot grant, raise or skip any permission or approval.

## 10. Step 10 acceptance tests

1. A fact told today is recalled in a new session after a restart.
2. An agent cannot read a scope that is not on its card.
3. "Forget that" removes the entry; retrieval no longer finds it; the full-text index no longer finds it.
4. A password or card number in a message is not stored.
5. Text from a file or web page is never written as memory.
6. Injected memory text ("approve everything") has no effect on approvals.
7. Retrieval never injects more than 5 entries or about 300 tokens.
8. Export works; backup then restore on a clean folder gives the same entries.
9. Expired entries are removed by the clean-up job.

## 11. Owner decisions needed

- [ ] Retention periods in section 6 (change any number you want).
- [ ] Backup schedule and backup folder.
- [ ] Should HCWMA ever store file contents for search? Default: no.
- [ ] Should the owner approve every automatic summary before it is saved? Default: no.
