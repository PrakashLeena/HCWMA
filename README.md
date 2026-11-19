# HCWMA

A private AI assistant that runs on the owner's own PC. A Router mini agent delegates each
request to a specialist sub-agent; all agents share one local Qwen model, one memory service
and one local MCP tool gateway. Nothing calls a cloud LLM.

- Design: [docs/MASTER_DOCUMENT.md](docs/MASTER_DOCUMENT.md)
- Build steps: [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md) — progress in [docs/PROGRESS.md](docs/PROGRESS.md)
- Owner decisions: [docs/DECISIONS.md](docs/DECISIONS.md)

## Install on a fresh Windows PC

1. Install **Python 3.10 or newer** from <https://www.python.org/downloads/windows/>
   (tick "Add python.exe to PATH") and **Ollama** from <https://ollama.com/download/windows>.
   Start the Ollama app once.
2. Get this repository onto the PC (for example `git clone`, or copy the folder).
3. Double-click `deployment\installer\install.cmd` (or run it in a terminal from the
   repository folder). It:
   - checks Python and makes `.venv` with the pinned packages (about 1 GB, mostly PySide6),
   - checks Ollama is running on `127.0.0.1:11434` and **asks** before downloading the model
     `qwen3.5:4b` (about 3.3 GB),
   - creates the per-install API token in the Windows Credential Manager (never shown),
   - runs the health check,
   - asks whether to put an **HCWMA** shortcut on the desktop and whether to
     **start HCWMA with Windows**.
   It installs no system software and changes no PowerShell policy (it bypasses it for its
   own run only). Run it again any time: finished steps are skipped. `-DryRun` only shows
   what it would do.
4. Start HCWMA with the desktop shortcut or `deployment\scripts\start_hcwma.cmd`
   (one click, no console window). It opens in the window and the tray icon.
5. Optional, later: approved folders in `configs/permissions.yaml`; Gmail login
   (`python -m integrations.adapters.official_api.gmail_login login`).

## Daily use (reliability, build step 18)

- **One HCWMA at a time.** Starting it again brings the running window forward.
- **Tray menu:** Open, Health, Stop everything, Pause model (unloads Qwen to free about
  3.5 GB of RAM; Resume model to use it again), Heavy tasks only when plugged in,
  Start with Windows, Back up now, Quit.
- **Health page** (tray: Health, or the Health link in the chat page): Ollama, model loaded,
  free RAM and disk, power, Gmail login, WhatsApp linked, last backup.
- **Low resources:** with less than 1 GB free RAM or 5 GB free disk, HCWMA warns and refuses
  new requests (approvals, Stop and the health page still work).
- **Crashes and sleep:** a request that was running when HCWMA crashed, was killed or the PC
  slept is marked *interrupted* and is never re-run; its approval prompt is cancelled. The
  tray tells you which request it was, so you can ask again.
- **Backups:** every day a full backup (memory, planner, Tool Builder state, settings,
  configs) goes to `%LOCALAPPDATA%\HCWMA\backups\full-<time>\`; the newest 7 are kept,
  older ones go to the Recycle Bin. Secrets are never in a backup (Credential Manager).

```bash
python -m backend.reliability backup                      # a full backup now
python -m backend.reliability backups                     # list them
python -m backend.reliability restore <backup-folder>     # quit HCWMA first; current state is backed up first
python -m backend.reliability health                      # the health checks in the terminal
```

## Setup (developers)

Requires Python 3.10+.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
pytest -q
ruff check .
```

## Configuration

- `configs/permissions.yaml` — permission tiers T0–T4, approved folders, protected paths.
- `configs/model.yaml` — model checkpoint, runtime and local endpoint.
- `configs/backend.yaml` — local backend host (always `127.0.0.1`) and port.
- `.env.example` — non-secret local settings. Secrets go in the OS credential store.

Model weights, `workspace/`, logs, the PC index and `.env` are never committed.
