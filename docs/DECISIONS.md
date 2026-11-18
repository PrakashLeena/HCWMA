# HCWMA Owner Decisions

Fill this in during Step 0. Claude Code reads it and must not guess missing values.

## PC

OS: Windows (version podu)
CPU: Intel i5-1135G7 (4 cores / 8 threads)
RAM: 16 GB
GPU: Intel Iris Xe (no NVIDIA) → CPU-only
Free SSD: ~546 GB
Laptop or desktop: Laptop



## Model

Qwen checkpoint: Qwen3.5-4B (fallback: Qwen3.5-2B)
License: Apache 2.0 (confirmed 2026-11-16 on the Hugging Face model card Qwen/Qwen3.5-4B and the Ollama license layer)
Runtime: Ollama (CPU)
Quantization: Q4_K_M



CPU-only, no NVIDIA GPU. Use Ollama with qwen3.5:4b (Q4).
Full safetensors copy already exists at `Qwen3.5-4B-Base/` (repo root, gitignored) — keep it for research only, do not load it for serving.

Step 3 result (2026-11-16): serving tag `qwen3.5:4b` on Ollama 0.40.16 = Q4_K_M, 3.3 GB download
(includes vision projector), supports tools, vision and thinking. Thinking mode is off for serving
(routing and tool calls stay fast). Kept as the daily model: ~8 tok/s generation, routing replies
in ~2.5 s, tool-call / JSON validity 9/9, ~3.5 GB RAM loaded (~4.8 GB peak). Low free RAM
(~1.4–1.9 GB) while loaded: close heavy apps, or switch to Qwen3.5-2B if the PC feels slow.
Numbers in `docs/benchmarks.md`.
## Access

- Folders HCWMA may index and read: none yet (approved list starts empty; decided 2026-11-16)
- Folders that are always off-limits (in addition to the protected paths): none extra (2026-11-16)
- HCWMA workspace folder: `workspace/` in the repo, gitignored (2026-11-16)

## Product

- Desktop shell (Tauri / Electron / PySide / browser-only for now): PySide6 (decided 2026-11-17; Python only, pinned pip install into `.venv`, no system software)
- T4 approval path: one-time owner approval per exact action (same task, tool and arguments), single use, expires after a few minutes, no "always allow"; `configs/permissions.yaml` defaults stay as they are (decided 2026-11-17)
- Daily-life agents wanted first:
- Self-built tools need owner approval before first use (default: yes):
- Image editing needs generative features (default: no):
- Languages for voice:
- Phone / second-device access wanted in this version (default: no):

## Memory (Step 10, decided 2026-11-17)

- Memory data folder: outside the repository, `%LOCALAPPDATA%\HCWMA` (`memory/` and `backups/`)
- Retention: as in `docs/memory-policy.md` section 6 (summaries 90 days, episodic 1 year,
  profile and skill notes until deleted, skill notes unused 180 days flagged, audit log 180 days)
- Backups: daily while HCWMA runs plus `python -m memory backup`; next to the database in
  `backups/`; newest 7 kept, older ones go to the Recycle Bin
- Automatic session summaries are saved without asking (owner can list and delete them)
- File contents for search: no (unchanged)
- Memory tool manifests (`memory_search`, `memory_write`, `memory_delete`,
  `memory_forget_all`, `reindex_folder`) approved by the owner

## Coding tools (Step 11, decided 2026-11-17)

- Coding tool manifests (`read_file`, `write_file`, `apply_patch`, `run_in_sandbox`, `run_tests`,
  `read_logs`) approved by the owner; they work only inside `workspace/code/`

## Image tools (Step 12, decided 2026-11-18)

- Image tool manifests (`image_info`, `resize_image`, `crop_image`, `convert_format`,
  `apply_filter`) approved by the owner; they write copies to `workspace/images/` only
- Generative image editing: no (default; expansion item)
