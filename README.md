# HCWMA v1.0

A private AI assistant that runs entirely on your own Windows PC. You type (or hold a key and
speak); a **Router** picks one specialist **agent**; the agent proposes tool calls; a policy
layer checks every call and asks you before anything risky. One local model (Qwen 3.5 4B via
Ollama, CPU only) serves every agent. Nothing is sent to a cloud AI.

Design: [docs/MASTER_DOCUMENT.md](docs/MASTER_DOCUMENT.md) · What it can do:
[docs/CAPABILITIES.md](docs/CAPABILITIES.md) · Decisions: [docs/DECISIONS.md](docs/DECISIONS.md) ·
Open items: [docs/BACKLOG.md](docs/BACKLOG.md)

## Requirements

- Windows 11 Pro (Windows Sandbox enabled for the Tool Builder), 16 GB RAM, ~15 GB free disk.
- Python 3.10 or newer, and [Ollama](https://ollama.com/download/windows).
- No GPU needed. Expect 40 s – 3 min per request on a laptop CPU.

## Fresh install

1. Install Python (tick "Add python.exe to PATH") and Ollama.
2. Copy or `git clone` this repository onto the PC.
3. Run `deployment\installer\install.cmd`. It makes `.venv` with the pinned packages, checks
   Ollama, **asks** before pulling `qwen3.5:4b` (~3.3 GB), creates the API token in Windows
   Credential Manager, runs the health check and offers a desktop shortcut and "Start with
   Windows". It installs no system software. `-DryRun` shows what it would do.
4. Optional:
   - Voice models (~700 MB): `.venv\Scripts\python -m voice download`
   - Gmail: `python -m integrations.adapters.official_api.gmail_login import-client <file>`, then `... login`
   - More folders HCWMA may search: `approved_folders` in `configs/permissions.yaml`

## Start

- Double-click the **HCWMA** shortcut, or `deployment\scripts\start_hcwma.cmd`, or
  `python -m app.desktop`. A window and a tray icon open. Only one HCWMA runs at a time.
- If Ollama is not running, HCWMA starts it (hidden, on 127.0.0.1 only).
- Tray: Health, Stop everything, Pause model (frees ~3.5 GB RAM), Heavy tasks only when
  plugged in, Start with Windows, Back up now, Quit.

## What each agent can do

| Agent | Can do | Asks you first |
|---|---|---|
| File & System | find files and folders, recent files, Recycle Bin, disk usage | restore, move, delete (to the Recycle Bin) |
| Documents & Study | read and summarize .txt/.md/.pdf/.docx, quizzes, flashcards, new Word files | — |
| Coding / Debugging | write, run and fix Python inside `workspace/code/` | — |
| Image Editing | resize, crop, convert, filter **copies** into `workspace/images/` | — |
| Web/Research | read a public web page by its link | — |
| App Automation | list open windows, read them | every click, typed text and key press |
| Communication | Gmail search/read/draft; WhatsApp Desktop list/read | every send |
| Planner | reminders, to-dos, deadlines with desktop notifications | — |
| Memory Librarian | remember, recall, forget what you ask | delete, forget all |
| System Settings | Wi-Fi/Bluetooth status and toggle, volume, brightness, theme | every change |
| Tool Builder | write small read-only tools, tested in Windows Sandbox | the new tool stays quarantined until you approve it on the Tools page |
| Voice | hold **F9** or the talk button; English, Tamil and Sinhala (Tamil and Sinhala experimental) | approvals are never given by voice |

## Safety rules

- **Approvals are clicks** on the card in the window: one action, once, expiring in minutes.
  Typed or spoken "yes" never approves anything.
- **Stop everything** (window or tray) halts every agent and withdraws every prompt.
- **Deleting** only ever moves to the Recycle Bin.
- **Protected paths** (credential stores, `~/.ssh`, browser profiles, system folders,
  `security/`) are never read or written. Agents only see approved folders and `workspace/`.
- Every tool call and delegation is in the audit log; secrets live only in Credential Manager.
- Everything listens on `127.0.0.1` only. Text from files, web pages and apps is treated as data,
  never as instructions.
- The microphone is open only while you hold the key; audio is not saved.

## Known limits (v1.0)

- Slow on CPU: most requests take 40 s – 3 min.
- Agents that must look something up never answer without a working tool call: you get
  "I could not do that: <reason>" instead. The Router never states the time, files, mail or
  settings itself. Some answers are still off target (see [docs/BACKLOG.md](docs/BACKLOG.md)).
- Ollama can grow to ~5.7 GB of RAM; below 1 GB free HCWMA refuses new requests. Tray →
  Pause model, then Resume model, frees it.
- Outlook is not supported (university admin consent blocks it). No SMS/Telegram.
- App Automation cannot open programs and does not know app names.
- Tamil voice is weak (~44% character errors), Sinhala voice not measured; push-to-talk works
  only while the HCWMA window is focused.
- No phone or second-device access, no home devices, no online calendar.

## Developers

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pytest -q
ruff check .
python scripts/smoke_test.py      # with HCWMA running: one safe request per agent
```

Build rules for Claude Code are in [CLAUDE.md](CLAUDE.md). Backups, restore and health from the
terminal: `python -m backend.reliability backup | backups | restore <folder> | health`.
