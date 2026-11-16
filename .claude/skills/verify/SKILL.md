---
name: verify
description: Run the HCWMA quality and security checks before a commit or at the end of a build step. Use before committing, when the owner asks "is this safe", or when build-step calls for verification.
---

# Verify before commit

Run every check. Report each as PASS or FAIL with the file and line for failures.
Fix what you can, then re-run. Never mark a check PASS without actually running it.

## Quality

1. `pytest -q` — all tests pass, none skipped without a written reason.
2. `ruff check .` — clean.
3. New code has tests. A new tool or agent has the test groups its skill requires.

## Security rules

4. **Localhost only.** Search for `0.0.0.0` and for server/host settings. Every listener
   binds to `127.0.0.1`.
5. **No secrets.** Search the diff for keys, tokens, passwords and `.env` values. Check
   `.gitignore` covers `.env`, `workspace/`, model weights, logs, the PC index and
   browser profiles. Run `git status` and confirm none of those are staged.
6. **No permanent delete.** Search for `os.remove`, `os.unlink`, `shutil.rmtree`,
   `Path.unlink`, `rmdir`. Allowed only inside tests on temp folders and inside the
   sandbox cleaner. Tool code uses `send2trash`.
7. **No direct execution by the model path.** Search for `subprocess`, `os.system`,
   `eval`, `exec`. Each use must sit inside a tool that goes through the policy layer.
8. **Protected paths enforced.** Every filesystem tool calls the shared path check.
9. **Allowlists enforced.** The agent runtime checks the card allowlist before every
   tool call, and the Router card has no tools.
10. **Audit.** Every new tool writes an audit record. Logs contain no secrets or file contents.
11. **Approvals.** Every T3/T4 tool is marked `approval_required` and is blocked in a test
    when unapproved.
12. **Limits.** Delegation depth and step budget are enforced in code, with a test.
13. **Quarantine.** Nothing in `integrations/generated/` is callable while `quarantined`.
14. **No cloud LLM.** No import of a hosted LLM SDK and no call to a remote model API.

## Report

End with a short table of the 14 checks and one line: `VERIFY: PASS` or
`VERIFY: FAIL (n issues)`. If any security check fails, do not commit.
