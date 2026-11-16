# HCWMA Build Progress

Tick a step only when its "Done when" test in `docs/BUILD_PLAN.md` passes.

## Milestone 1 — the factory works with one agent

- [] Step 0 — Planning and decisions (`docs/DECISIONS.md` filled in)
- [] Step 1 — PC foundation (tools installed, repo created)
- [x] Step 2 — Development environment
- [x] Step 3 — Qwen baseline running locally
- [ ] Step 4 — Source exploration (optional, does not block)
- [x] Step 5 — Backend and chat UI
- [x] Step 6 — Tool foundation (MCP, permissions, audit)
- [x] Step 7 — Agent runtime and Router
- [ ] Step 8 — File & System agent

## Milestone 2 — safe and useful

- [ ] Step 9 — Approval workflow and stop control
- [ ] Step 10 — Persistent memory
- [ ] Step 11 — Coding and Debugging agents
- [ ] Step 12 — Image Editing, Web/Research, App Automation agents
- [ ] Step 13 — Tool Builder agent

## Milestone 3 — full assistant

- [ ] Step 14 — Voice
- [ ] Step 15 — Daily-life agents and first real app integration
- [ ] Step 16 — Multi-device access (optional)
- [ ] Step 17 — Home/device integrations
- [ ] Step 18 — Reliability
- [ ] Step 19 — Expansion

## Notes

Add one line per finished step: date, commit, anything the next step should know.

- 2026-11-16 Step 2: package skeleton (master doc §12), pinned requirements (pytest, ruff, pyyaml), configs/permissions.yaml (no approved folders yet, strict defaults) and configs/model.yaml (Ollama, CPU, 127.0.0.1:11434). Steps 0 and 1 are not ticked yet: the Product section of DECISIONS.md is still blank, and the owner should confirm `claude --version`. Step 3: confirm the Ollama tag `qwen3.5:4b` and the license; Qwen3.5-4B-Base/ inside the repo is gitignored and kept for research only.
- 2026-11-17 Step 3: Ollama `qwen3.5:4b` (Q4_K_M, CPU, thinking off) behind `model/serving/` and `agents/runtime/model_queue.py` (one request at a time). Numbers in `docs/benchmarks.md`; owner confirmed the terminal chat. Step 5: call the model only through `model_queue.py`.
- 2026-11-17 Step 5: FastAPI backend on 127.0.0.1:8765 (`python -m backend.api`), `/health` open, `/chat` needs `Authorization: Bearer <token>`; token in the OS credential store via keyring (`python -m backend.auth.token`). Chat page in `app/web/` keeps the token per tab (sessionStorage) and holds the history client-side. `/chat` calls the model queue directly with no tools: Step 7 should put the Router behind it. Step 9: the desktop shell should hand the token to the page so the owner need not paste it. Later: Starlette's test client deprecates `httpx` in favour of `httpx2`.
- 2026-11-17 Step 6: official `mcp` SDK 2.3.0, stdio only (`python -m integrations.mcp.server`, spawned by the gateway; no socket). `agents/runtime/policy.py` `PolicyLayer` = catalogue lookup → `PermissionManager.check` (allowlist, approved status, tier, approval) → arg validation → MCP call → audit (`security/audit/logs/audit.jsonl`). New manifests default to `quarantined`; `get_system_time` (T0) is approved. Check by hand: `python -m integrations.mcp.smoke`. Step 7: build `AgentGrant` from each card; policy is async (wrap for the sync model queue); `AgentGrant.delegated_from` intersects grants — decide how it applies to Router→agent delegation, since the Router has no tools; use `AuditLogger.delegation` for delegations. Step 8: every filesystem tool must call `PathPolicy.check` and indexers prune with `is_protected`. Step 9: T4 is refused even when approved; the explicit T4 approval path is still to decide.
- 2026-11-17 Step 7: cards in `agents/cards/` (schema `agents/registry/card.py`, `kind: router` may hold no tools), `AgentRegistry.from_dir()`, `TaskEnvelope`, `AgentRuntime` (any card; tools only via `PolicyLayer`; tool output fenced as `<tool_output>` data), `TaskBudget` (depth 3, 20 tool calls shared across the whole request), Router + JSON-schema classifier (confidence < 0.6 → clarifying question). `/chat` now goes through the Router (MCP gateway lives in the app lifespan). Router→agent delegation gives the target its own card grant (the Router has none to intersect), capped at T1 when `read_only` (always true until Step 9); agent→agent uses `delegated_from`. Check by hand: `python -m agents.router.demo` (PASS on Qwen) and `python -m agents.router.eval_routing` (13/14 scorable cases; 26 wait for their agents). `agents/prompts/router.md` and `tests/agents/routing_cases.yaml` were drafted by Claude Code (the Prepare step had not produced them) — owner to review. Speed: a routing decision takes ~13 s on this CPU (prefill ~35 tok/s, Ollama does not reuse the prompt prefix); the catalogue grows with each card, so Step 18 (or sooner) should look at prefix caching, a compact catalogue or a 2B router (master doc 5.5). Step 8: disable `echo` (status: disabled) once File & System covers real requests; add file_system via `add-agent` — no Router change needed. Step 9: `needs_approval` results stop the task; resume-after-approval is not built. Step 10: Router does not pull or write memory yet. Step 13: nothing-fits → Tool Builder is not wired; the Router answers "cannot do that yet".
