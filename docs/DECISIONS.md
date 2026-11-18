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

## App Automation tools (Step 12, decided 2026-11-18)

- Desktop tool manifests (`list_windows`, `read_window_text`, `find_ui_element`, `click_element`, `type_text`, `press_key`) approved by the owner; click/type/key ask the owner every time (T3)
- Browsers, banking, password managers, terminals, HCWMA's own windows and password fields stay off-limits; Playwright / browser download: not approved

## Reviewed exceptions to "never permanent delete" (architecture rule 9)

1. 2026-11-17: `record.unlink()` in `integrations/adapters/pc/windows.py` removes the Recycle Bin's
   own `$I...` bookkeeping record after a successful `restore_file`.
2. 2026-11-18 (Step 13 sandbox): `WindowsSandbox._remove_registry()` in
   `integrations/adapters/pc/windows_sandbox.py` unlinks the sandbox's own `active.json` (the
   record of the running VM) in its fixed state folder `workspace/sandbox/runs/`. It asserts the
   resolved path equals exactly that file (and is not a link) before deleting; any other path is
   refused (`test_registry_delete_refuses_any_other_path`).

## Step 13 carry-over (owner, 2026-11-18)

- The Tool Builder's sandbox-test tool wrapper must call `PathPolicy` on the tool folder before
  staging (verify check 8) and write an audit record for every sandbox run (check 10).

## Tool Builder (Step 13)

Owner rules (2026-11-18, from the Step 13 request): v1 generated tools are read-only
(`side_effects: none`; no files, secrets, shell, subprocess or ctypes); network only to hosts in
the manifest through the shared web fetch; tests only in Windows Sandbox; any changed byte sends
a tool back to quarantine; the owner sees the summary, the permissions and the full code.

Design choices made by Claude Code, **owner to confirm or change**:

- Approval state is kept in `%LOCALAPPDATA%\HCWMA\generated_tools\state.json` (outside the
  repository, next to memory; `HCWMA_DATA_DIR` overrides it), per exact content hash.
- The owner picks which agents may use an approved tool when approving it (agent cards are
  not edited); the tool is always T1.
- The four Tool Builder tool manifests (`find_existing_tools`, `scaffold_tool`, `run_tool_tests`,
  `submit_for_approval`) ship `quarantined` until the owner approves them
  (`TOOL_BUILDER_TOOLS_STATUS` in `integrations/adapters/local_data/tool_builder_tools.py`).

Owner decisions on the review (2026-11-18), all three implemented:

1. **Model-queue race.** While a sandbox run is active the backend must not reload Qwen. The run
   takes a lock file `model_busy.json` in `%LOCALAPPDATA%\HCWMA` (`agents/runtime/model_lock.py`;
   never deleted, released by writing `active: false`; it also stops counting after its time limit
   or when its owner process is gone). `ModelQueue.generate` and `/chat` refuse while it is held:
   "busy: testing a tool, try again shortly". Released only after Qwen is reloaded.
2. **`exec` exception, approved on one condition.** `integrations/generated_tools/runner.py` calls
   `exec` on an APPROVED tool's code, in a separate child process (guard hook, restricted
   builtins, time and memory limit, scrubbed environment). The condition: immediately before
   `exec` the runner itself recomputes the SHA-256 of the files it was sent and compares it with
   the approved hash, and re-runs the static check on that exact code; either failing means
   nothing runs. The gateway side also re-reads the files and the approval right before starting
   the child. Tests: a file changed after approval is refused. This is the only place that execs
   model-written code; the verify skill's "no exec" rule is unchanged (this is a reviewed exception).
3. **Protected path.** `~/AppData/Local/HCWMA/generated_tools` is in `protected_paths`
   (`configs/permissions.yaml`, a stricter change). An approved tool is callable only if BOTH the
   owner assigned the agent at approval AND the policy layer allows it (allowlist, approved
   status, tier); tests cover each half failing alone.
