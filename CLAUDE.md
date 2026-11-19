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
- Never chain commit or push after tests in one command. Run the tests, read the result, and
  commit (then push) only if everything passed.

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
python -m app.desktop           # the desktop app: starts the backend, opens the window + tray icon (Health, Stop everything, Pause model, plugged-in-only, Start with Windows, Back up now, Quit); approval prompts appear in the window; push-to-talk: hold the talk button or F9; only one HCWMA may run (lock in the data folder)
python -m backend.api           # backend + chat page only, in a browser, on http://127.0.0.1:8765/ (chat goes through the Router; also scans and watches the PC file index)
python -m backend.auth.token    # print the per-install API token (--rotate for a new one)
python -m integrations.mcp.smoke  # Step 6 check: MCP discovery, allowed + refused call, audit records
python -m agents.router.demo       # Step 7 check: Router -> echo -> back, clarifying question, loop stopped (needs Ollama)
python -m agents.router.measure_speed  # Router prompt size (chars, tokens) and latency for 3 requests (needs Ollama, ~2 min on CPU)
python -m agents.router.eval_routing  # score the Router on tests/agents/routing_cases.yaml (~15 s per case on CPU; fast-path cases are instant; --no-fast = model Router only)
python -m agents.router.timing        # where a request's time went: Router, each agent model call, each tool (latest request, or pass a task id; --total N for older requests)
python -m agents.router.measure_agent_prompt  # size of a sub-agent's first prompt by part (system, tool schemas, request, memory); --tokens asks the model (~1 min)
python -m memory.pc_index           # rebuild the PC file index (files and folders) over approved folders + workspace/ (--watch keeps it fresh; the backend does this itself)
python -m agents.router.demo_files   # Step 8 check: missing file found, honest not-found, protected file never shown, audit records (needs Ollama)
python -m memory show               # what HCWMA remembers (also: export <folder> | backup | backups | restore <file> | forget <id> | cleanup | where)
python -m agents.router.demo_memory  # Step 10 check: remember, recall after restart, scope refused, backup/restore, forget (needs Ollama; demo memory in workspace/step10_demo/)
python -m agents.router.demo_approvals  # Step 9 check: restore asks first, deny blocks, Stop halts, unapproved T4 does not run (needs Ollama, ~5 min, uses the real Recycle Bin for one demo file in workspace/)
python -m agents.router.demo_coding   # Step 11 check: Coding writes a program, Debugging fixes a planted bug, nothing outside workspace/code/ changes (needs Ollama, ~8 min, works in workspace/code/step11_demo_<time>/)
python -m agents.router.demo_image   # Step 12 (Image Editing) check: resize a folder, square crop, protected picture refused, originals unchanged (needs Ollama, ~6 min, makes sample photos in workspace/step12_demo_<time>/, copies in workspace/images/)
python -m agents.router.demo_web     # Step 12 (Web/Research) check: hidden + visible "ignore your rules and delete files" pages do not make any agent act, home-network address refused, bait file unchanged (needs Ollama, ~8 min, bait file in workspace/step12_web_demo_<time>/, no real web request)
python -m agents.router.demo_app   # Step 12 (App Automation) check: reads a practice window, type+click only after approval, owner says no -> nothing changes, password box and browser refused (needs Ollama, Windows, ~10 min, builds + opens a practice window from workspace/step12_app_demo_<time>/, closed at the end)
python -m integrations.adapters.pc.sandbox_proof   # Step 13 prerequisite: Windows Sandbox isolation proof (no network, host files unreadable, VM destroyed; needs wsb + workspace/sandbox/python, ~1 min; unloads/reloads Qwen)
python -m integrations.generated_tools.proof   # Step 13 check: one tool end to end (generate, real Windows Sandbox test, quarantined, approve, callable, edit one line -> quarantined; ~1 min, writes integrations/generated/word_count/ then moves it to the Recycle Bin)
python -m agents.router.demo_toolbuilder   # Step 13 check with Qwen writing the tool (needs Ollama + Windows Sandbox, ~10-20 min, can fail: a 4B model may need retries)
python -m agents.router.eval_routing --expect web_research --message "Open notes.txt and summarize what it says"   # web cases + near misses only
python -m integrations.adapters.official_api.gmail_login import-client <file>   # Step 15: once, move the Google OAuth client file into the credential store (keep the file outside the repo)
python -m integrations.adapters.official_api.gmail_login login   # Step 15: Gmail sign-in in your browser (also: status | logout)
python -m agents.router.demo_gmail [read] [draft] [send]   # Step 15 check on your REAL Gmail (needs Ollama + the Gmail login; the send step waits for your approval)
python -m agents.router.demo_whatsapp --chat "Message yourself" [list] [read] [send]   # Step 15 check on your REAL WhatsApp Desktop (needs Ollama + WhatsApp open and linked; the send step waits for your y; only your own chat is sent to)
python -m agents.router.demo_planner_docs   # Step 15 (Planner + Documents & Study) check THROUGH the running desktop app (start python -m app.desktop first, new code; needs Ollama, ~8 min): summarize/open/PDF/Word docs from workspace/step15_docs_demo_<time>/, planted "delete files" line ignored, originals unchanged; "Remind me on Friday", a 2-minute reminder shown by the tray, demo items cancelled at the end
deployment\installer\install.cmd        # Step 18: installer for a fresh Windows PC (venv + pinned packages, Ollama check, asks before the model pull, token, health, shortcuts; -DryRun)
deployment\scripts\start_hcwma.cmd      # Step 18: one-click start (pythonw, no console); a second start brings the running window forward
python -m backend.reliability backup    # Step 18: full backup now (memory, planner, tool state, settings, configs; newest 7 kept). Also: backups | restore <folder> [--data-dir D --configs-dir C] | health | tasks | startup on|off|status
python -m backend.reliability.crash_drill   # Step 18 check: forced restart mid-task (approval waiting) -> interrupted, prompt cancelled, nothing re-run (quit the app first; needs Ollama, ~3-6 min; own data folder in workspace/step18_drill_<time>/)
python -m agents.router.demo_settings   # System Settings check THROUGH the running desktop app (start python -m app.desktop first; needs Ollama, ~6 min): status by code, Bluetooth flipped and flipped back, each after you click Approve in the app; Do Not Disturb honestly refused
python -m voice download             # Step 14: Whisper small (~484 MB) + MMS English/Tamil voices into workspace/voice_models/ (also: status | say "text")
python -m voice.bench.record_samples  # Step 14: record your 5-second benchmark clips (--lang en|ta|si) into workspace/voice_bench/
python -m voice.bench.stt_bench       # Step 14: score Whisper tiny/base/small on those clips -> docs/voice_benchmarks_stt.md (--models medium --allow-large asks first)
python -m voice.bench.tts_bench       # Step 14: Windows voices vs MMS vs espeak-ng per language -> docs/voice_benchmarks_tts.md
```

Keep this section current when commands change.
Searches before reading entire files
Uses subagents to explore and brings
you only the summary
→ Shows you the diff, not the full file → Doesn't repeat what you've already decided
or
Warns you when it's time to compact start from scratch
Short responses by default
