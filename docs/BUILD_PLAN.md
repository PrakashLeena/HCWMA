# HCWMA Build Plan — Step by Step

How to get from the master document to a working HCWMA, one step at a time.
Design source of truth: `docs/MASTER_DOCUMENT.md` (v1.1).

---

## 1. Who does what

There are three workplaces. Use each for what it is good at.

| Workplace | Use it for | Do not use it for |
|-----------|-----------|-------------------|
| **Claude (chat app)** | Thinking and deciding: choosing the Qwen checkpoint for your PC, design questions, drafting agent prompts and capability cards, writing routing test cases, reviewing a permission policy, explaining an error or a concept, updating the master document | Writing the project's code files one by one and copy-pasting them |
| **Claude Code (on your PC, inside the HCWMA folder)** | Building: creating files, writing code and tests, installing dependencies, running the model, running tests, debugging, committing to Git | Big open-ended design decisions with no written plan |
| **You (by hand)** | Installing software and drivers, recording your PC specs, approving downloads and permission prompts, trying each finished step yourself, making the final call on every decision | — |

Two points that are easy to mix up:

- **Claude Code builds HCWMA. HCWMA itself runs on Qwen.** Claude is the builder, not a part of the finished assistant.
- **Claude Code must run on your own PC, in the project folder.** HCWMA needs your GPU and your files, so the code has to be run and tested there.

### The loop for every step

```
1. Claude (chat)   → prepare: decide, draft prompts/cards/test cases for this step
2. You             → paste the results into docs/ (or DECISIONS.md)
3. Claude Code     → /build-step <n>
4. You             → try the "Done when" test yourself
5. Claude Code     → commits, ticks docs/PROGRESS.md
6. Start a NEW Claude Code session for the next step
```

### Working rules

- One step per Claude Code session. Finish, commit, then start fresh.
- Never skip a "Done when" test. A step that is not proven is not done.
- If Claude Code wants to change the design, bring the question to Claude (chat), decide, update the master document, then continue.
- Push to a private GitHub repository after every step. That is your backup and your undo.

---

## 2. What is in this starter kit

```
hcwma/
  CLAUDE.md                         # rules Claude Code reads every session
  docs/
    MASTER_DOCUMENT.md              # the design (v1.1)
    BUILD_PLAN.md                   # this file
    PROGRESS.md                     # step checklist
    DECISIONS.md                    # your answers; fill in during Step 0
  .claude/skills/
    build-step/SKILL.md             # /build-step <n>  — build one step properly
    add-agent/SKILL.md              # /add-agent <name> — add a sub-agent
    add-tool/SKILL.md               # /add-tool <name>  — add an MCP tool or adapter
    verify/SKILL.md                 # /verify           — tests + 14 security checks
    model-setup/SKILL.md            # /model-setup      — choose, install, benchmark Qwen
```

Claude Code also loads these skills on its own when the task matches, so plain requests like "add an image agent" work too.

---

## 3. The steps

Steps follow the phases in master document section 13.
Each step lists: **Where**, **Prepare**, **Build**, **Done when**.

---

### MILESTONE 1 — the factory works with one agent (Steps 0–8)

#### Step 0 — Planning and decisions

- **Where:** You + Claude (chat)
- **You do:**
  1. Collect your PC specs: OS version, CPU, RAM, GPU model and VRAM, free SSD space. On Windows: Task Manager → Performance, or run `nvidia-smi` and `systeminfo` in a terminal.
  2. Ask Claude (chat): *"Here are my PC specs: … Which current official Qwen checkpoint and quantization fit this machine for HCWMA, and which runtime should I use? Check the official model cards and licenses."*
  3. Decide which folders HCWMA may read, and which are off-limits.
  4. Fill in `docs/DECISIONS.md`.
- **Done when:** every line in `docs/DECISIONS.md` has an answer (use "later" only for voice and multi-device).

#### Step 1 — PC foundation

- **Where:** You (by hand)
- **You do:**
  1. Update the OS and GPU driver.
  2. Install Git, Python (a version supported by current PyTorch), Node.js and VS Code.
  3. Install Claude Code and sign in.
  4. Create a private GitHub repository named `hcwma`, clone it, and unzip this kit into it.
  5. Make the first commit and push.
- **Done when:** `git --version`, `python --version`, `node --version` and `claude --version` all answer, `nvidia-smi` shows your GPU (if you have one), and the kit is pushed to GitHub.

#### Step 2 — Development environment

- **Where:** Claude Code
- **Build:** `/build-step 2`
  - Create the folder structure from master document section 12 (empty packages with `__init__.py`).
  - Virtual environment, `requirements.txt` with pinned versions, `pytest`, `ruff`, `.gitignore`, `.env.example`, `README.md`.
  - `configs/permissions.yaml` and `configs/model.yaml` as templates filled from `DECISIONS.md`.
- **Done when:** on a fresh clone, the setup commands in `CLAUDE.md` work and `pytest -q` passes with one placeholder test.

#### Step 3 — Qwen baseline running locally

- **Where:** Claude Code (you approve the download)
- **Build:** `/build-step 3` (it uses the `model-setup` skill)
  - Install the runtime, download the chosen checkpoint, write the `model/serving/` wrapper and the benchmark.
- **Done when:** `docs/benchmarks.md` shows repeatable numbers (memory, speed, tool-call JSON validity) for your PC, and you have chatted with the model once from the terminal.
- **If it is too slow or runs out of memory:** take the numbers to Claude (chat) and choose a smaller checkpoint or quantization, then run `/model-setup` again.

#### Step 4 — Source exploration (optional, does not block)

- **Where:** Claude (chat) to explain, Claude Code to set up
- **Build:** clone the Transformers repository beside the project, open the Qwen model directory, and ask Claude to walk you through config, attention and generation code.
- **Done when:** you have a notes file `docs/model-notes.md`. Skip this for now if you want a working assistant sooner; come back any time.

#### Step 5 — Backend and chat UI

- **Where:** Claude Code
- **Build:** `/build-step 5`
  - FastAPI app bound to `127.0.0.1`, per-install token, `/health` and `/chat` endpoints, model calls through `model_queue.py`.
  - A simple web chat page served locally. The desktop wrapper (tray icon, start with PC) comes in Step 9.
- **Done when:** you open the local page, send a message, get a Qwen reply; a request without the token is rejected; nothing listens on any address except `127.0.0.1`.

#### Step 6 — Tool foundation

- **Where:** Claude Code
- **Prepare (Claude chat):** ask for a review of your `configs/permissions.yaml`: approved folders, protected paths, tiers.
- **Build:** `/build-step 6`
  - Tool schema base classes, capability catalogue, local MCP server and client, permission manager (tiers T0–T4), shared path check, audit logger.
  - One harmless test tool, for example `get_system_time`.
- **Done when:** the test tool is discovered and called through MCP, an audit record is written, and a call from an "agent" without permission is refused.

#### Step 7 — Agent runtime and Router

- **Where:** Claude (chat) first, then Claude Code
- **Prepare (Claude chat):**
  - *"Draft the Router system prompt for HCWMA following master document section 4.2."*
  - *"Write 40 routing test cases: user message → expected agent, including unclear ones that should trigger a clarifying question."*
  - Save them as `agents/prompts/router.md` and `tests/agents/routing_cases.yaml`.
- **Build:** `/build-step 7`
  - Card schema and loader, agent registry, task envelope, agent runtime (runs any agent from its card), limits (depth 3, 20 tool calls), Router.
  - A dummy `echo` agent to prove delegation.
- **Done when:** a request goes Router → echo agent → back with a result; an unclear request produces a clarifying question; a looping task is stopped by the limits.

#### Step 8 — File & System agent (first real agent)

- **Where:** Claude (chat) first, then Claude Code
- **Prepare (Claude chat):** *"Draft the capability card and system prompt for the File & System agent (master document 4.3, 4.4, 4.6)."*
- **Build:** `/build-step 8`
  - PC file index over approved folders (SQLite with full-text search), kept fresh by a file watcher.
  - Read-only tools: `search_files`, `list_recent_files`, `check_recycle_bin`, `get_disk_usage`.
  - The agent itself, added with the `add-agent` skill.
- **Done when:** you type *"I'm missing one of my project files, it was called …"* and HCWMA finds it (or honestly says it cannot), the audit log shows every tool call, and a file under a protected path never appears in results.

**Milestone 1 reached.** The whole pattern works: Router → sub-agent → tool → result. Everything after this repeats the pattern.

---

### MILESTONE 2 — safe and useful (Steps 9–13)

#### Step 9 — Approval workflow, stop control, desktop shell

- **Where:** Claude Code
- **Build:** `/build-step 9`
  - Approval service: T3/T4 tool calls pause and show a prompt with what will happen; you approve or deny.
  - "Stop everything" control that halts all agents.
  - Write tools for the File agent: `restore_file`, `move_file`, `delete_file` (Recycle Bin only).
  - Wrap the web UI in the desktop shell chosen in `DECISIONS.md`, with a tray icon.
- **Done when:** restoring a file asks you first; denying it blocks the action; Stop halts a running task; an unapproved T4 action cannot run by any route.

#### Step 10 — Persistent memory

- **Where:** Claude (chat) to design, Claude Code to build
- **Prepare (Claude chat):** decide what HCWMA may remember about you, for how long, and how you delete it. Write it to `docs/memory-policy.md`.
- **Build:** `/build-step 10`
  - Memory service with the layers in master document section 8, scope checks per agent, summaries, the Memory (Librarian) agent, export and delete.
- **Done when:** a fact told today is recalled in a new session tomorrow; an agent cannot read a scope not on its card; "forget that" really removes it; backup and restore work.

#### Step 11 — Coding and Debugging agents

- **Where:** Claude (chat) for the two cards and prompts, then Claude Code
- **Build:** `/build-step 11`
  - Sandbox workspace, tools `read_file`, `write_file`, `run_in_sandbox`, `run_tests`, `read_logs`, `apply_patch`.
- **Done when:** HCWMA writes a small working program in the workspace, and fixes a bug you planted in a test project, without writing anywhere outside the workspace.
- **Expect:** a small local model is far weaker at coding than Claude. Keep these agents' jobs narrow and test what they really manage on your checkpoint.

#### Step 12 — Image Editing, Web/Research and App Automation agents

- **Where:** Claude Code, one agent per session
- **Build:** `/build-step 12` three times, or `/add-agent image_editing`, `/add-agent web_research`, `/add-agent app_automation`
  - Image tools on Pillow / OpenCV (writes copies, never overwrites originals).
  - Playwright for web; isolated browser profile stored outside the repository.
  - Desktop UI automation at T3 (always asks).
- **Done when:** each agent completes one scripted task; a web page containing hidden instructions does not make an agent act on them.

#### Step 13 — Tool Builder agent

- **Where:** Claude (chat) to review the design, then Claude Code
- **Prepare (Claude chat):** *"Review master document section 6.6. What could go wrong with a self-built tool on my PC, and what tests should prove the quarantine works?"*
- **Build:** `/build-step 13`
  - Scaffold generator, sandbox test runner, manifest validator, quarantine status, owner review screen, approve / disable / roll back.
- **Done when:** you ask for something no tool covers; the Tool Builder writes a tool; it cannot be called while quarantined; after you approve, it works; changing its permissions sends it back to quarantine.

---

### MILESTONE 3 — full assistant (Steps 14–19)

#### Step 14 — Voice

- **Where:** Claude (chat) to pick STT/TTS for English, Tamil and Sinhala, then Claude Code
- **Build:** `/build-step 14` — push-to-talk first; STT → Router → TTS.
- **Done when:** a spoken request gets a spoken answer, measured on your own recorded samples in each language.

#### Step 15 — Daily-life agents and first real app integration

- **Where:** Claude (chat) to choose the first app and check it has an official API, then Claude Code
- **Build:** `/add-agent communication`, `/add-agent planner`, `/add-agent documents_study`, and `/add-tool` for the chosen app.
- **Done when:** a read-only task on the real app works; anything that sends, posts or deletes asks you first.

#### Step 16 — Multi-device access (optional)

- **Where:** Claude (chat) to decide if you need it, then Claude Code
- **Done when:** only a device you registered can connect, over LAN or VPN, never the open internet.

#### Step 17 — Home/device integrations

- **Where:** Claude Code, one device type per session with `/add-tool`.
- **Done when:** each device action respects permissions and is logged.

#### Step 18 — Reliability

- **Where:** Claude Code
- **Build:** `/build-step 18` — backups and a restore drill, crash recovery, sleep/wake handling, low-disk and low-battery behaviour, pause/unload the model, installer.
- **Done when:** the app survives a forced restart mid-task and restores from backup on a clean folder.

#### Step 19 — Expansion

- More agents and tools, faster serving, fine-tuning only if measurements justify it, generative image editing, a dedicated server if you want HCWMA always on.

---

## 4. When something goes wrong

| Situation | What to do |
|-----------|-----------|
| Claude Code is stuck on an error | Ask it to explain the cause before changing more code. If still stuck, paste the error into Claude (chat) for a second opinion. |
| A step turns out much bigger than expected | Stop. Ask Claude (chat) to split it into two or three smaller steps and update this plan. |
| The model gives bad routing or broken tool calls | Run `/model-setup` to benchmark; improve the prompt and the card's `handles` list before blaming the code. |
| A test keeps failing | Never delete it. Find out whether the test or the code is wrong. |
| You want to change the design | Decide in Claude (chat), update `docs/MASTER_DOCUMENT.md`, then tell Claude Code. |
| Something broke that worked before | `git log`, go back to the last step's commit, and rebuild the step in a fresh session. |
