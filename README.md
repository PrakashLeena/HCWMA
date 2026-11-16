# HCWMA

A private AI assistant that runs on the owner's own PC. A Router mini agent delegates each
request to a specialist sub-agent; all agents share one local Qwen model, one memory service
and one local MCP tool gateway. Nothing calls a cloud LLM.

- Design: [docs/MASTER_DOCUMENT.md](docs/MASTER_DOCUMENT.md)
- Build steps: [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md) — progress in [docs/PROGRESS.md](docs/PROGRESS.md)
- Owner decisions: [docs/DECISIONS.md](docs/DECISIONS.md)

## Setup

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
