# HCWMA Build Progress

Tick a step only when its "Done when" test in `docs/BUILD_PLAN.md` passes.

## Milestone 1 — the factory works with one agent

- [ ] Step 0 — Planning and decisions (`docs/DECISIONS.md` filled in)
- [ ] Step 1 — PC foundation (tools installed, repo created)
- [x] Step 2 — Development environment
- [ ] Step 3 — Qwen baseline running locally
- [ ] Step 4 — Source exploration (optional, does not block)
- [ ] Step 5 — Backend and chat UI
- [ ] Step 6 — Tool foundation (MCP, permissions, audit)
- [ ] Step 7 — Agent runtime and Router
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
