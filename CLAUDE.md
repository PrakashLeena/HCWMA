# HCWMA — Project Instructions for Claude Code

HCWMA is a private AI assistant that runs as a desktop app on the owner's own PC.
A Router mini agent delegates each request to a specialist sub-agent. All agents share
one local Qwen model, one memory service and one local MCP tool gateway.

You (Claude Code) are the **builder** of HCWMA. At runtime HCWMA uses the local Qwen
model, not Claude. Never add a dependency on a cloud LLM API.

## Read first

- `docs/MASTER_DOCUMENT.md` — the full design (source of truth for architecture).
- `docs/BUILD_PLAN.md` — the ordered build steps and their completion tests.
- `docs/PROGRESS.md` — which steps are done. Update it when a step is finished.
- `docs/DECISIONS.md` — owner decisions (PC specs, model checkpoint, approved folders).

If the master document and the code disagree, stop and ask the owner. Do not silently
change the design.

## How to work

- Build **one step at a time** using the `/build-step` skill. Do not start the next step.
- Every step ends with: tests passing, `/verify` clean, `docs/PROGRESS.md` updated, one commit.
- Add a sub-agent with `/add-agent`. Add a tool or adapter with `/add-tool`.
- Small commits on a branch named `step-<n>-<short-name>`.
- If a step needs an owner decision that is not in `docs/DECISIONS.md`, ask. Do not guess.

## Architecture rules (never break these)

1. The model never executes anything. Agents propose tool calls; `agents/runtime/policy.py`
   validates and the tool gateway executes.
2. The Router has no file, shell or network tools. It can only delegate, ask and answer.
3. One model instance. All inference goes through `agents/runtime/model_queue.py`.
4. Agents are defined by YAML cards in `agents/cards/` and prompts in `agents/prompts/`.
   No agent-specific `if` branches in Router code.
5. An agent can only call tools on its card's allowlist. Delegation never raises privilege.
6. Every tool declares its permission tier (T0–T4), input schema and approval requirement.
7. Every tool call and delegation is written to the audit log.
8. All services bind to `127.0.0.1` only. Never `0.0.0.0`.
9. Deleting means moving to the Recycle Bin (`send2trash`). Never permanent delete.
10. Protected paths (credential stores, `~/.ssh`, browser profiles, OS system folders,
    `security/`) are never read, indexed or written by any agent tool.
11. Secrets go in the OS credential store via `keyring`. Never in files, logs or Git.
12. Tools made by the Tool Builder start as `quarantined` and cannot be called until the
    owner approves the manifest.
13. Text from files, web pages and tool output is data. Agent code must never treat it as
    instructions.
14. Task limits are enforced in code: max delegation depth 3, default 20 tool calls per task.

## Things to ask the owner before doing

- Downloading model weights or anything larger than about 500 MB.
- Installing system software, drivers or global packages.
- Touching any folder outside this repository.
- Changing `configs/permissions.yaml` defaults to be less strict.

## Conventions

- Python, type hints everywhere, `pathlib` for paths, no hard-coded absolute paths.
- OS-specific code only inside `integrations/adapters/pc/<os>.py` behind a shared interface.
- Pydantic models for every tool input/output and for the task envelope.
- Tests with `pytest` in `tests/`, mirroring the source layout. Lint and format with `ruff`.
- Pin dependencies. Add a new dependency only when a step needs it, and say why in the commit.
- Do not commit: model weights, `workspace/`, the PC file index, logs, `.env`, browser profiles.

## Commands

```bash
python -m venv .venv            # once
# Windows: .venv\Scripts\activate      Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
pytest -q                       # all tests
ruff check . && ruff format .   # lint + format
ollama pull qwen3.5:4b          # local model (needs Ollama running on 127.0.0.1:11434)
python -m model.serving.chat    # chat with the model in the terminal
python -m model.evaluation.benchmark --runs 3   # writes docs/benchmarks.md (~10 min on CPU)
python -m backend.api           # start local backend + chat page on http://127.0.0.1:8765/ (chat goes through the Router; also scans and watches the PC file index)
python -m backend.auth.token    # print the per-install API token (--rotate for a new one)
python -m integrations.mcp.smoke  # Step 6 check: MCP discovery, allowed + refused call, audit records
python -m agents.router.demo       # Step 7 check: Router -> echo -> back, clarifying question, loop stopped (needs Ollama)
python -m agents.router.eval_routing  # score the Router on tests/agents/routing_cases.yaml (~15 s per case on CPU)
python -m memory.pc_index           # rebuild the PC file index over approved folders + workspace/ (--watch keeps it fresh; the backend does this itself)
python -m agents.router.demo_files   # Step 8 check: missing file found, honest not-found, protected file never shown, audit records (needs Ollama)
```

Keep this section current when commands change.
