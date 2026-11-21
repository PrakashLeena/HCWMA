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

- Folders HCWMA may index and read: `~/Documents`, `~/Desktop`, `~/Downloads`, `E:/ML/project`
  (decided 2026-11-18; the list started empty on 2026-11-16), plus `~/OneDrive/Desktop` and
  `~/OneDrive/Documents` (decided 2026-11-19: Windows keeps the real Desktop and Documents in
  OneDrive). Indexing reads names, dates and sizes only and never opens a file, so OneDrive
  cloud-only files are not downloaded.
- Folders that are always off-limits (in addition to the protected paths): none extra (2026-11-16)
- HCWMA workspace folder: `workspace/` in the repo, gitignored (2026-11-16)

## Product

- Desktop shell (Tauri / Electron / PySide / browser-only for now): PySide6 (decided 2026-11-17; Python only, pinned pip install into `.venv`, no system software)
- T4 approval path: one-time owner approval per exact action (same task, tool and arguments), single use, expires after a few minutes, no "always allow"; `configs/permissions.yaml` defaults stay as they are (decided 2026-11-17)
- Daily-life agents wanted first:
- Self-built tools need owner approval before first use (default: yes):
- Image editing needs generative features (default: no):
- Languages for voice: English, Tamil, Sinhala (2026-11-19). Push-to-talk only. STT Whisper small (CPU int8, loaded only while talking); TTS MMS for English and Tamil, espeak-ng for Sinhala (owner's choice after docs/voice_benchmarks_stt.md and _tts.md). Tamil and Sinhala are experimental.
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

### Tool Builder tool manifests approved (owner, 2026-11-18)

The owner approved the four manifests (`find_existing_tools` T0, `scaffold_tool` T2,
`run_tool_tests` T2, `submit_for_approval` T2) after these tests passed
(`tests/integrations/generated_tools/test_lifecycle.py`):

1. The four tools can only record "tested" / "submitted" in the state file; no code in the
   Tool Builder's tool module can reach `approve`, `revoke`, `set_disabled`, `restore_files` or
   the review service, and a repo-wide test lists the only files that call them (the review
   service, the token-protected backend routes, and the proof/demo scripts that play the owner).
2. `scaffold_tool` accepts only names matching `^[a-z][a-z0-9_]{2,40}$` (no "..", slashes,
   drive letters or dots) and refuses Windows device names. It refuses to overwrite a tool that
   is currently approved: the approved files stay untouched and in use, and a new version must be
   built under a new name (e.g. `word_count_v2`), which starts quarantined.
3. `scaffold_tool` refuses files over the fixed limits, counted in bytes: manifest 8 KB, adapter
   20 KB, tests 20 KB (stricter than the 50 KB suggested); nothing is written when refused.

`TOOL_BUILDER_TOOLS_STATUS` is now `"approved"`.

## Gmail / Communication agent (Step 15, decided 2026-11-19)

- First real app: Gmail, official Gmail API, OAuth desktop flow. The OAuth client file lives
  outside the repository (`C:\Users\kibox\HCWMA-secrets`); only its id and secret were
  imported into the Windows credential store (`HCWMA` / `gmail_oauth_client`). The login is the
  refresh token (`gmail_refresh_token`); the owner's address is kept as `gmail_account_address`.
- Scopes: `gmail.readonly` and `gmail.compose` only (no delete, settings or contacts).
- Gmail tool manifests (`search_messages`, `read_message`, `draft_message`, `send_message`)
  approved by the owner; `send_message` asks every time. A recipient must be an address the owner
  typed or the owner's own address.
- Outlook: a later session, separate adapter. **Update 2026-11-20 (owner):** blocked — the
  university Microsoft account requires admin consent for the app, so Outlook is not in v1.0.

## WhatsApp / Communication agent (Step 15, decided 2026-11-19)

- WhatsApp through WhatsApp Desktop and the UI Automation layer only: no unofficial WhatsApp
  libraries, no web scraping.
- The WhatsApp Desktop window is allowed for the Communication agent only; App Automation keeps
  it off-limits (`COMMUNICATION_ONLY_APPS` in `integrations/adapters/desktop/ui_interface.py`).
- Tools: `whatsapp_list_chats` T1, `whatsapp_read_chat` T1 (one chat the owner named),
  `whatsapp_send` T3 (approval every time, chat name + full text; the chat must be one the owner
  typed; one message per approval and per request). Manifests approved by the owner 2026-11-19.
- No bulk sending, no auto-replies, no scheduled messages, no reading chats the owner did not ask
  about. Chat text is untrusted data. Before sending, the open chat's header must match the
  requested name, otherwise stop.

## System Settings agent (Step 15, decided 2026-11-19)

- **Reversible Windows toggles are T3, not T4**: the owner approves EVERY change, each time.
  The read-only status tool (`get_settings_status`) is T1. Tool manifests set by the owner in the
  add-agent request (`set_radio`, `set_volume`, `set_brightness`, `set_theme` T3 + approval;
  `get_settings_status` T1).
- Allowed actions only: Wi-Fi on/off, Bluetooth on/off, volume level and mute, screen brightness,
  dark/light mode, battery saver and Do Not Disturb (see below). Everything else in Windows
  settings stays denied (T4): nothing that needs admin rights, and never firewall, Defender,
  accounts, updates, drivers, DNS, network configuration or the registry. No tool exists for
  any of these.
- Official Windows APIs only, never UI clicking: WinRT `Windows.Devices.Radios` (Wi-Fi,
  Bluetooth), Core Audio `IAudioEndpointVolume` via comtypes (volume), WMI
  `WmiMonitorBrightnessMethods` (built-in screen brightness), WinRT `PowerManager` (battery
  saver status, read only).
- **Dark/light mode: one narrow registry exception** (owner, 2026-11-19). Windows has no API for
  it, so `set_theme` writes only the two per-user values `AppsUseLightTheme` and
  `SystemUsesLightTheme` under `HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize`
  (names are constants in `integrations/adapters/pc/windows_settings.py`) and broadcasts
  `WM_SETTINGCHANGE "ImmersiveColorSet"`. Nothing else in the registry is ever written.
- **Battery saver and Do Not Disturb: status only, no toggle** (owner, 2026-11-19). Windows has
  no public API to switch them (only undocumented internals or UI clicking, both ruled out).
  Battery saver status is read; the agent tells the owner to use Quick Settings (Win + A).
- New dependencies (owner approved): `winrt-runtime`, `winrt-Windows.Devices.Radios`,
  `winrt-Windows.Foundation`, `winrt-Windows.Foundation.Collections`,
  `winrt-Windows.System.Power` (small, venv only).

## Reliability (Step 18, owner request 2026-11-19)

Owner rules (from the Step 18 request): one HCWMA at a time; Start with Windows is an owner
toggle (off by default); a task running at a crash or PC sleep is marked interrupted and never
re-run, pending approvals are cancelled; daily full backup, keep 7, restore command proven on
a clean folder; health page; refuse new heavy tasks below 1 GB free RAM or 5 GB free disk;
"Pause model" unloads Qwen; optional "heavy tasks only when plugged in"; installer for a fresh PC.

Design choices made by Claude Code, **owner to confirm or change**:

- "Heavy task" = every chat request (each one runs Qwen). Approvals, Stop, reminders and the
  health page are light and always work. The 1 GB / 5 GB limits can only be raised
  (`settings.json`), never lowered; free disk is the lowest of the data folder's drive and
  the repository's drive.
- Pause model also blocks new requests until Resume model (otherwise the next request would
  load Qwen straight back). It is not remembered across restarts.
- The task journal (`<data dir>/tasks/journal.sqlite3`) keeps the first 80 characters of each
  request so the owner can recognise an interrupted one; newest 500 rows kept.
- Sleep is detected by a wall-clock jump of more than 60 s between 5-second ticks.
- Full backups (`backups/full-<time>/`) sit next to the existing daily memory-only backups
  (`memory-*.sqlite3`, Step 10, unchanged). A memory "forget" also cleans the memory copy in
  every full backup. Restore also puts the repository's `configs/*.yaml` back (after a
  checksum check), and refuses while HCWMA is running.
- Start with Windows = one `HCWMA.lnk` in the owner's own Startup folder (no registry, no
  service); turning it off moves it to the Recycle Bin.
- The single-instance lock also refuses to reuse a backend that answers on the port without
  holding the lock (an HCWMA from before Step 18), instead of silently using old code.

Found on 2026-11-19: drive C: (where `%LOCALAPPDATA%\HCWMA` lives) has **4.9 GB free**, below
the 5 GB limit, so HCWMA refuses new requests until space is freed on C: (owner to act; the
"Free SSD ~546 GB" above is out of date).

## Local model runtime (decided 2026-11-20)

- If Ollama is not running when HCWMA needs the model (nothing answers on 127.0.0.1:11434),
  HCWMA starts the installed `ollama.exe serve` itself: hidden, detached, OLLAMA_HOST=127.0.0.1:11434
  (never 0.0.0.0), OLLAMA_MODELS from `ollama_models_dir` in configs/model.yaml (E:/ollama/models).
- ollama.exe is found on PATH or in the standard install folders; HCWMA never downloads or installs it.
- One start attempt at a time (lock); requests wait up to 60 s (`ollama_start_timeout_s`) while the
  chat shows "Starting the local model…". A failure gives one message with the reason and what to do,
  and is not retried for 60 s (no loop). Also started at HCWMA launch; shown on the Health page.

## Voice RAM (decided 2026-11-20)

- Whisper small needs 1340 MB free (measured peak 1038 MB + 300 MB margin). Less free: Qwen is unloaded
  for the transcription (same model-busy lock as the sandbox) and reloaded before the Router call; the
  extra time is shown in the chat. Still not enough: the top 5 apps by RAM are named so the owner can
  close some. HCWMA never closes, kills or trims any process.

## Web tools and grounded answers (decided 2026-11-20)

- Web/Research tool manifests `open_website` and `extract_page_information` approved by the owner
  (they had stayed `quarantined` since Step 12, so the agent could not read pages and invented
  answers). Every other tool manifest was already approved; checked 2026-11-20.
- Agents whose card says `requires_tool: true` (web_research, file_system, communication,
  documents_study, image_editing, planner, system_settings, app_automation, echo, tool_builder)
  may not answer without one successful tool call. The runtime replaces such an answer with
  "I could not do that: <reason>" (tool not approved yet / tool failed / no tool used). Coding,
  Debugging and the Memory Librarian may answer from the request or recalled memory.

## Auto-launch needed apps (decided 2026-11-20)

- New tool `launch_app` (T2, no approval prompt, manifest approved by the owner): opens ONE
  installed app from the owner's list in `configs/apps.yaml`. Starting list: WhatsApp,
  Calculator, Notepad. Anything not on the list is refused. Used by App Automation.
- Each list entry is pinned to its package + publisher (start of the AppUserModelID found with
  `Get-StartApps`), so a look-alike shortcut is never started. Opened like a Start-menu click
  (`explorer.exe shell:AppsFolder\<id>`). HCWMA never downloads or installs an app.
- Off-limits apps can never be added, whatever the YAML says (dropped when the list is read):
  web browsers, password managers, terminals / code editors / anything that runs commands,
  system security and settings, banking and payment apps, app stores and installers, HCWMA.
- `launch_app` opens only an app the owner named in their own request.
- The WhatsApp tools open WhatsApp Desktop themselves when it is not running (or running with
  no window), show "Opening WhatsApp…" in the chat, wait for the chat list, then
  continue. On the QR / link screen they stop with "WhatsApp needs to be linked: scan the QR
  code on your phone"; linking is never automated.
- One launch at a time (lock shared by all tools). One attempt: a failed app is not tried again
  for 60 s, and every failure gives one plain message (not installed / did not open in time).
- Wait per app (`wait_s` in configs/apps.yaml): 30 s by default; WhatsApp 90 s, because on this
  PC WhatsApp shows its loading splash for 30-60+ s after launch (measured 2026-11-20; slower
  while Qwen is loaded).
- A WhatsApp window that opens behind others is brought to the front once while it loads.
- Opening WhatsApp does not change who may use it: App Automation still may not click or type
  in it (`COMMUNICATION_ONLY_APPS`).

## App inventory, launch permission, adapters, first-run wizard (decided 2026-11-21)

- **App inventory.** HCWMA scans the installed apps at first run, at every start (in the
  background) and once a day, and on the owner's "Scan again". Read-only sources: `Get-StartApps`
  (AppUserModelIDs of Store and desktop apps), `Get-AppxPackage` (publisher, package family),
  Start Menu shortcuts (target program) and the installed-programs registry keys. Saved per PC
  as `%LOCALAPPDATA%\HCWMA\app_catalog.json` (never in Git). Registry entries that point at
  installers, updaters or runtimes are left out.
- **Launch permission.** `launch_app` (T2) can open any catalog app the owner named. If the app
  is not yet allowed, the desktop app shows a card "HCWMA wants to open <app>" with **Allow once
  / Always allow / Deny**. Only an owner click answers it (never voice, never an agent; the
  `/approvals` route needs the per-install token, and "always" is refused on any card that does
  not offer it). "Always allow" is saved per app in `%LOCALAPPDATA%\HCWMA\app_permissions.json`
  and can be revoked on the Apps page (`/apps`). It covers opening only: clicks, typing and sends
  inside an app still need per-action approval. Every decision is audited (`approval` and
  `app_access` records).
- **Hard block in code** (`integrations/apps/blocklist.py`): browsers, password managers and
  authenticators, banking / payment / crypto apps, terminals and anything that runs commands
  (code editors, Python, Git Bash…), system and security tools (Settings, Control Panel,
  Registry Editor, programs in the Windows folder…), remote-control tools, app stores and
  installers, and HCWMA itself. They never show a card; "Always allow" and `apps.yaml` cannot
  unblock them (checked again at launch, since the catalog file is data).
- **`configs/apps.yaml` becomes owner overrides**: an entry opens without a card and is pinned to
  its package + publisher. Shipped with WhatsApp only (used by the WhatsApp adapter); Calculator
  and Notepad moved to the normal catalog + card flow.
- **Adapters.** Each bundled adapter declares the app it serves (package family / program name /
  publisher): WhatsApp (WhatsApp Desktop) and Gmail (an API, no app). After a scan HCWMA offers
  "WhatsApp found: enable the WhatsApp adapter?". An adapter that is off has its tools refused
  and hidden by the gateway. On/off is per PC in `%LOCALAPPDATA%\HCWMA\adapters.json`. A new
  install starts with every adapter off; an install that already had data keeps both adapters on
  (written once at the first start of this version, audited) so the update breaks nothing.
- **No downloaded adapters.** HCWMA never downloads adapters or any other code from the
  internet. Signed adapter packs only, later. Apps without an adapter are used through App
  Automation, which asks before every click and keystroke.
- **First-run wizard** (`/setup`, shown once until finished or skipped; reopen from the Apps
  page): PC check (RAM, disk, Ollama, model), app scan and "may open without asking" selection,
  adapters (on/off; Gmail login status and how to sign in; WhatsApp linking hint), and approved
  folders: the real Desktop / Documents / Downloads are read from the "User Shell Folders"
  registry key (detects OneDrive redirection); only those detected folders can be added to
  `approved_folders`, on an owner click, and take effect after a restart.
- The four per-PC files are protected paths for agent tools (`configs/permissions.yaml`).
- **Block-list gaps closed (2026-11-21, owner).** The owner's "Always allow" choices stay as
  they are; these categories are hard-blocked anyway and never open, even if allowed:
  - **Browsers**: any program Windows has registered as a web browser (`Clients\StartMenuInternet`,
    `RegisteredApplications` entries handling http/https, the http/https default), matched by
    program, AppUserModelID or name, read from the registry at every scan and again at launch;
    plus Comet and other browser names.
  - **Code runners**: IDEs and editors that run code (Antigravity, NetBeans, Dev-C++, VS Code,
    JetBrains IDEs, Python, Node.js, Git Bash…) and VMs / emulators (VirtualBox, VMware,
    BlueStacks…).
  - **System / security / network**: Windows Backup, Check For Updates, Malwarebytes and
    antivirus, VPN clients (Cloudflare One / WARP and others), PowerToys, and Ollama.
  On the owner's PC this blocked 14 previously allowed entries; every other allowed app is
  unchanged.
  - **Borderline apps, owner's choice**: ChatGPT / OpenAI Codex (coding agent), GitHub Copilot,
    CoD RconTool (remote console; its "Server Status" program in the same folder too), About
    Java / Configure Java (system config), MySQL Workbench and MongoDB Compass (can change
    databases). Microsoft Copilot and Gemini stay openable.
- **"Open <app>" is answered by code (2026-11-21, owner).** When `launch_app` succeeds and the
  owner's whole request was to open that app ("open Paint", "please launch Paint for me"), the
  answer is "Opened Paint." built by code: no second model call, no `list_windows`. When the
  request also asks for something inside the app, `launch_app` waits up to 15 s for the app's
  window (`window_ready` in its result) and the model goes on with the next step.

## Model RAM (backlog item 1, decided 2026-11-21)

- Measured (`docs/ram_soak.md`): llama-server holds ~5.0 GB private right after loading Qwen and
  ~5.5 GB after a few different prompts, then stays flat; working set ~3.7-4.2 GB. Not `num_ctx`
  (already fixed at 8192 on every chat request) and not parallel slots (Ollama already ran `-np 1`).
- The `ollama serve` HCWMA starts itself gets `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=1`,
  `LLAMA_ARG_CACHE_RAM=512` (prompt cache, default 8192 MiB) and `LLAMA_ARG_CTX_CHECKPOINTS=4`
  (default 32 x ~50 MiB). An Ollama started by its own tray app keeps Ollama's defaults.
- The warm-up load (`OllamaBackend.load`) and `measure_speed` send the same `num_ctx` as every request,
  so no request reloads the model at a different context size.
- Idle RAM guard (`backend/reliability/model_control.py` `RamGuard`, checked every 30 s by the backend):
  no model call for 5 minutes (`idle_unload_after_s`) and llama-server above 5300 MB private
  (`idle_unload_ram_mb`) -> the model is unloaded (audit record `unload_model`); it is not paused, the
  next request reloads it (~9 s). Never during a model call; does nothing while the owner has paused it.
  Works with either Ollama. The Health page shows the process RAM next to "Model loaded".
- Not done (owner decision): a text-only Ollama tag without the vision projector (~0.9 GB less);
  HCWMA does not send images to Qwen today.

