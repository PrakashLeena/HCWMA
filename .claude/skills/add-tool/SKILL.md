---
name: add-tool
description: Add a new MCP tool or application adapter to HCWMA (PC access, official API, custom API, browser, desktop). Use when an agent needs a capability that is not in the tool catalogue.
---

# Add a tool or adapter

Tool to add: $ARGUMENTS

Design reference: `docs/MASTER_DOCUMENT.md` sections 6.1–6.6, 9.3 and 9.4.

## 1. Pick the route

Follow the decision tree in section 6.3, in this order:

1. An approved tool already exists → use it, stop.
2. Official API / OAuth → `integrations/adapters/official_api/`
3. Documented or authorized endpoint, plugin, CLI or local interface → `custom_api/` or `local_data/`
4. The task is about the PC itself → `integrations/adapters/pc/`
5. Accessible UI and automation is permitted → `browser/` (Playwright) or `desktop/`
6. None of these → tell the owner it is unsupported. Do not look for a way around logins,
   MFA, CAPTCHA, paywalls or a service's terms.

## 2. Define the contract

- Pydantic input and output models. Validate every argument (paths, URLs, sizes, limits).
- A manifest entry with: `name`, `version`, `route`, `description`, `inputs`, `outputs`,
  `permissions` (network hosts, filesystem roots, secrets), `side_effects`
  (`none` / `writes` / `external_action`), `permission_tier`, `approval_required`.
- Tier guide: T0 metadata, T1 read, T2 write inside workspace, T3 write outside workspace
  or app automation, T4 delete / install / settings / credentials.
- Any tool with `side_effects` other than `none` outside the workspace needs approval.

## 3. Implement

- Path arguments: resolve the real path, then check it is inside an approved folder and
  not under a protected path. Reject symlink escapes.
- Network: only the hosts in the manifest.
- Secrets: read through `keyring` at call time; never log them, never return them.
- Deleting: `send2trash` only.
- Set a timeout. Return a clear error object on failure; never raise raw exceptions to the agent.
- Write one audit record per call: task id, agent, tool, argument summary, outcome.
- OS-specific code goes behind the shared adapter interface.

## 4. Register

- Add the tool to the capability catalogue and expose it through the local MCP server.
- Add it to the allowlist of the agent cards that need it, and no others.
- Tools written by the Tool Builder agent at runtime go in `integrations/generated/<name>/`
  with status `quarantined`. Tools you write here during development may be `approved`
  only after the owner has seen the manifest.

## 5. Test

In `tests/integrations/test_<name>.py`:

- success case,
- invalid arguments rejected,
- permission denied for an agent without the tool on its allowlist,
- protected path / unapproved folder rejected (for filesystem tools),
- timeout handled,
- expired or missing credential handled (for API tools),
- approval-required action blocked when not approved,
- an audit record is written.

Use temp folders and mocked network in tests. Tests must never touch the owner's real
files or accounts.

## Done when

All tests pass, the tool shows up in MCP discovery, and an agent without permission
cannot call it.
