# HCWMA — Home AI Assistant & Universal Integration Platform

## Project Master Document — Local Agent Factory Edition

**Version 1.2 | Updated: 20 November 2026 | Supersedes v1.1 (v1.2 = as built for HCWMA v1.0)**

Architecture • Qwen • Local PC • Agent Factory • API / MCP • Self-Built Tools • Automation • Memory • Voice • Security • Roadmap

---

## Changes from v1.1 (v1.2 — as built for HCWMA v1.0)

v1.2 records what was actually built. Where a later section still describes a v1.1 plan
(roadmap rows, risks, references), this table wins. Marked inline with *(v1.2: …)*.

| # | Area | v1.1 plan | v1.2 as built |
|---|------|-----------|---------------|
| D1 | Model and hardware | Quantized Qwen sized to the PC's GPU | Ollama `qwen3.5-text:4b` (the `qwen3.5:4b` Q4_K_M weights without the vision projector, 2026-11-21), **CPU only** (i5-1135G7, 16 GB, Iris Xe), thinking off; models in `E:\ollama\models`; HCWMA starts `ollama serve` itself when it is down |
| D2 | Routing | Router classifies every request with the model | Plus a **fast path**: card-defined rules route very clear requests with no model call; short replies continue a pending clarification; unmatched requests are not sent to the Tool Builder |
| D3 | PC access | Files, apps, shell, screen | Files, Recycle Bin, disk usage, code workspace, window UI Automation, selected settings. **No shell or screen-capture tool** |
| D4 | Tool Builder testing | "A sandbox" | **Windows Sandbox** (network off, VM destroyed), Qwen unloaded during the run; tools start **quarantined**; approval by content hash; any edit re-quarantines |
| D5 | Email and messages | Official APIs first | **Gmail** via the official API (OAuth + PKCE, tokens in Credential Manager); **WhatsApp Desktop via UI Automation**; **Outlook blocked by university admin consent** (not built) |
| D6 | System settings | T4, default deny | **System Settings agent**: status T1; reversible toggles (Wi-Fi/Bluetooth radio, volume, brightness, dark/light) **T3, approved every time**; everything else has no tool |
| D7 | Browser / app automation | Playwright + accessibility APIs | No Playwright. Web = read-only httpx + HTML parser; desktop = Windows UI Automation on already-open windows; browsers and password fields refused |
| D8 | Voice | Evaluate STT/TTS; push-to-talk first | **Push-to-talk only** (F9 or button, in the window). STT faster-whisper **small**, CPU int8, loaded only while talking (1340 MB free needed, else Qwen unloaded and reloaded). TTS **MMS** (English, Tamil), **espeak-ng** (Sinhala). Tamil/Sinhala experimental |
| D9 | Approved folders | To be chosen | `~/Documents`, `~/Desktop`, `~/Downloads`, `E:/ML/project`, **`~/OneDrive/Desktop`, `~/OneDrive/Documents`**, plus `workspace/` |
| D10 | Desktop shell | Tauri recommended | **PySide6** window + tray; per-install API token in Credential Manager instead of an owner account |
| D11 | Memory | SQLite, Qdrant later | SQLite FTS5 in `%LOCALAPPDATA%\HCWMA`; file index has names/sizes/dates only; no RAG or Qdrant |
| D12 | Reliability | — | Single instance, crash/sleep recovery (interrupted tasks never re-run), daily full backup (keep 7), health page, low-RAM/disk refusal, installer |
| D14 | Grounded answers | — | Cards with `requires_tool` cannot answer without a successful tool call ("I could not do that: <reason>"); `live_facts` on the cards stop the Router stating the time, files, mail or settings itself |
| D13 | Not built in v1.0 | — | Multi-device access, home devices, online calendar, Outlook, wake word, global hotkey, RAG |

---

## 0. What Changed From v1.0

v1.0 stays the base. Only the points below changed; everything else carries forward as written.

| # | Area | v1.0 | v1.1 |
|---|------|------|------|
| C1 | Where it runs | Dedicated home server (Ubuntu Server) | The user's own PC, as a local desktop application. No separate server. |
| C2 | Agent design | One agent controller | An **agent factory**: one Router mini agent plus many specialist sub-agents |
| C3 | Model usage | One Qwen model server for one controller | One local Qwen model shared by every agent (agents differ by role, tools and knowledge, not by model) |
| C4 | Memory | Persistent memory service | Same service, now shared by all agents, plus per-agent knowledge cards and a PC file index |
| C5 | PC access | Desktop automation was one of four integration routes | PC access is a core capability: files, apps, shell, screen, under tiered permissions |
| C6 | Tool creation | Developers write every adapter by hand | A Tool Builder agent can generate, test and register its own API adapters and MCP tools, with owner approval |
| C7 | Hardware / OS | New server under LKR 500,000, Ubuntu Server, SSH, UFW, VPN | Existing PC; services bound to localhost. Server purchase becomes an optional later upgrade |
| C8 | Multi-device access | Core phase | Optional later phase (PC acts as the host) |

Section tags used below: **[UNCHANGED]**, **[CHANGED]**, **[NEW]**.

Assumptions made in this revision (confirm or correct, see section 16):

- A1. "Agents for what a human needs" is read as **daily-life agents** (communication, planning, study/documents, home devices).
- A2. The target PC runs Windows. The design is kept cross-platform so Linux/macOS also work.
- A3. The Tool Builder may write and test tools on its own, but a new tool only becomes usable after the owner approves it once.

---

## Document Contents

1. Executive Summary
2. Vision, Goals and Non-Goals
3. Requirements and Operating Principles
4. Overall System Architecture (Agent Factory)
5. Qwen Model, Source Code and Customization
6. Universal Application Connector: API, MCP, Automation and Self-Built Tools
7. Voice, Language and Multi-Device Access
8. Persistent Memory and Knowledge
9. Security, Privacy and Safety
10. Local PC Hardware and Operating System
11. Recommended Software Stack
12. Proposed Repository / Folder Structure
13. Step-by-Step Development Roadmap
14. Testing and Acceptance Criteria
15. Risks and Mitigations
16. Project Decisions and Open Items
17. Official References and Documentation
18. Final Recommendation

---

## 1. Executive Summary [CHANGED]

HCWMA (Home-Centred World Model Assistant) is a private, extensible AI assistant that runs as a **desktop application on the user's own PC**. No separate server is built. Its language model is an open-source Qwen family model running locally.

HCWMA is organized as an **agent factory**. A small Router agent receives each request, works out what kind of task it is, and hands it to the specialist sub-agent best suited to it (file recovery, coding, debugging, image editing, and so on). All agents share one local Qwen model, one persistent memory and one tool gateway. When no existing tool can do a job, a Tool Builder agent can create the needed API adapter or MCP tool itself.

The main design decision from v1.0 is kept: the Qwen model stays largely separate from the execution system. The model understands requests and selects tools; HCWMA's Python services execute approved actions.

> **Core principle:** Qwen is the reasoning interface; HCWMA services are the execution, memory, integration and security layer. Agents are roles on top of that one model, not separate models.

---

## 2. Vision, Goals and Non-Goals

### 2.1 Vision [CHANGED]

- Build a private AI assistant that lives on the user's PC and can work with the machine: files, documents, code (in a workspace), open application windows and selected settings. *(v1.2: no general shell or screen-capture tool was built.)*
- Organize it as a factory of specialist agents coordinated by a Router mini agent, so new skills are added by adding an agent, not by rewriting the system.
- Support natural-language and voice commands, with a path toward English, Tamil, Sinhala and mixed-language usage.
- Use an open-source Qwen model locally, with the ability to inspect its implementation and customize the surrounding system.
- Connect to websites, online services, desktop applications, local files, databases and home devices through approved interfaces.
- Let the system build its own API adapters and MCP tools when a needed capability is missing.
- Retain useful long-term context in a separate memory system shared by all agents, rather than relying only on the model's context window.
- Keep the owner in control of what each agent may do.
- Keep the architecture modular so new models, agents, tools and integrations can be added later.

### 2.2 Non-goals and realistic boundaries [CHANGED]

- HCWMA will not automatically gain access to every application or private service merely because a custom API or MCP server is created, whether a developer or the Tool Builder agent created it.
- It will not bypass authentication, MFA, CAPTCHA, access controls, paywalls, rate limits or service terms.
- Changing model source code does not automatically add reliable new knowledge or capabilities; fine-tuning, training, tools or external services may be required.
- Training a frontier-scale model from scratch is out of scope. The plan is to use and customize an existing open model.
- **New:** "Access to the whole PC" does not mean unrestricted action. Read access is broad; changes, deletions and system settings pass through the permission tiers in section 9.
- **New:** Agents do not each load their own model. A normal PC cannot hold several large models at once.
- **New:** HCWMA is available only while the PC is on and the application is running.

---

## 3. Requirements and Operating Principles

### 3.1 Functional requirements [CHANGED]

- Local model inference with a replaceable model/runtime.
- Desktop application with a conversation interface.
- **Router mini agent** that classifies each request and delegates it.
- **Specialist sub-agents**, each with a defined role, tool allowlist and knowledge card.
- **Agent registry** so agents can be added, disabled or updated without code changes elsewhere.
- Tool calling through MCP and direct service adapters.
- **Tool Builder** capability: generate, test and register new API adapters / MCP tools.
- Persistent memory with search and retrieval, shared by all agents.
- **PC file index** for fast search across approved folders.
- Voice input and speech output as separate services.
- Authorized browser and desktop automation.
- Integration with approved APIs, local files, databases and selected smart devices.
- Per-agent and per-action permissions.
- Audit logs for tool use, delegation and sensitive actions.
- Backup, update, health monitoring and clean start/stop with the PC.

### 3.2 Design principles [CHANGED]

- **Least privilege:** each agent and integration receives only the permissions it needs.
- **Human approval:** require confirmation for sending messages, purchases, payments, deleting data, changing security settings or other high-impact actions.
- **Modularity:** model, agents, memory, voice, tools and integrations are separate components.
- **Reversibility:** prefer read-only operations first; provide undo/rollback where supported.
- **Observability:** log which agent ran which tool, when, and whether it succeeded.
- **Local-first:** keep data on the PC; use external services only when needed and authorized.
- **New — One brain, many roles:** a single model instance serves every agent through a request queue.
- **New — Bounded delegation:** every task has a step budget and a maximum delegation depth so agents cannot loop forever.
- **New — Resource-aware:** the PC is also the user's working machine. HCWMA must be able to pause or unload the model when the user needs the RAM. *(v1.2: CPU only, no GPU; tray "Pause model"; voice unloads Qwen when RAM is short.)*

---

## 4. Overall System Architecture (Agent Factory) [CHANGED]

The model still never controls the operating system directly. An agent proposes a named tool call; the policy layer validates it, checks that agent's permissions and invokes the adapter.

```
USER (desktop app: text / voice)
        ↓
HCWMA DESKTOP APP  (UI + local owner authentication)
        ↓
ROUTER MINI AGENT  (understand request → pick agent → delegate → collect result)
        ↓
AGENT REGISTRY  (capability cards: who can do what)
        ↓
┌──────────────────── SPECIALIST SUB-AGENTS ────────────────────┐
│ File & System │ Coding │ Debugging │ Image Editing │ Web/Research │
│ App Automation │ Memory (Librarian) │ Tool Builder │ Daily-Life   │
└───────────────────────────────────────────────────────────────┘
        ↓                         ↕
POLICY + APPROVAL LAYER     SHARED QWEN MODEL (one instance, queued)
        ↓                         ↕
TOOL GATEWAY / MCP CLIENT   SHARED MEMORY + PC FILE INDEX
        ↓
LOCAL MCP SERVER(S) + INTEGRATION REGISTRY
   ├── PC access tools (files, Recycle Bin, disk, code workspace, window UI Automation, settings)
   ├── Official API adapters
   ├── Custom API adapters (documented/authorized interfaces)
   ├── Self-built tools (generated by Tool Builder, owner-approved)
   ├── Web reading (httpx + HTML parser; no Playwright)
   ├── Desktop / accessibility automation
   └── Home-device / network integrations (not built in v1.0)
        ↓
THE PC, APPROVED APPLICATIONS, SERVICES AND DEVICES
```

### 4.1 Main components [CHANGED]

| Component | Responsibility |
|-----------|----------------|
| Qwen model | Understand language, reason over context, propose tool calls, produce responses. Shared by all agents. |
| Inference runtime | Load and serve the model locally; manage GPU/CPU memory, queueing and quantization. |
| Router mini agent | Classify the request, choose the sub-agent, pass the task, merge results, ask for clarification when unsure. |
| Agent registry | Hold each agent's capability card, tool allowlist, permission tier and status. |
| Sub-agents | Carry out one kind of work each, using only their allowed tools. |
| Tool Builder agent | Create missing API adapters / MCP tools, test them, submit them for approval. |
| Memory service | Store and retrieve facts, summaries, documents, task context and the PC file index. |
| Voice services | Push-to-talk STT (faster-whisper small), TTS (MMS / espeak-ng). No wake word. |
| MCP/tool gateway | Expose approved capabilities to agents through a standard tool interface. |
| Application adapters | Translate tool requests into each application's supported operations. |
| Policy / security service | Owner identity, per-agent permissions, approvals, secrets, audit and rate limits. |
| Desktop app shell | Window, tray icon, approval prompts, start/stop with the PC. |

### 4.2 The Router mini agent [NEW]

The Router is deliberately small. It does not do the work; it decides who does.

Router loop:

1. Receive the request and pull relevant memory.
2. Classify intent and match it against capability cards in the agent registry.
3. If one agent clearly fits, delegate. *(v1.2: very clear requests are routed by card-defined fast-path rules with no model call.)* If several steps are needed, split into sub-tasks and delegate each in order.
4. If nothing fits, ask the user to clarify or say it cannot do that yet. *(v1.2: the Router does not hand unmatched requests to the Tool Builder.)*
5. Collect results, check the task is actually complete, reply to the user, write the outcome to memory.

Router rules:

- The Router has no file, shell or network tools of its own. It can only delegate, ask and answer.
- Maximum delegation depth: 3. Default step budget per task: 20 tool calls. Both configurable.
- Low routing confidence triggers a clarifying question instead of a guess.

### 4.3 Sub-agent catalogue [NEW]

| Agent | What it does | Example tools | Default tier (see 9.4) |
|-------|--------------|---------------|------------------------|
| File & System | Find, recover, move, delete files; Recycle Bin; disk usage | `search_files`, `list_recent_files`, `check_recycle_bin`, `get_disk_usage`, `restore_file`, `move_file`, `delete_file` | T0/T1 read; T3 restore/move; T4 delete (Recycle Bin); all three ask |
| Coding | Write, run and explain Python in `workspace/code/` | `read_file`, `write_file`, `apply_patch`, `run_in_sandbox`, `run_tests` | T2, workspace only |
| Debugging | Reproduce errors, read logs, propose and test fixes | `read_file`, `read_logs`, `run_tests`, `run_in_sandbox`, `apply_patch` | T2, workspace only |
| Image Editing | Resize, crop, convert, filter copies | `image_info`, `resize_image`, `crop_image`, `convert_format`, `apply_filter` | T1 info; T2 writes copies to `workspace/images/` |
| Web / Research | Read a public page by its link and summarize it (no search engine) | `open_website`, `extract_page_information` | T1 + network; manifests approved 2026-11-20 |
| App Automation | Read and operate windows that are already open (Windows UI Automation) | `list_windows`, `read_window_text`, `find_ui_element`, `click_element`, `type_text`, `press_key` | T1 read; T3 click/type/key, each approved |
| Memory (Librarian) | Store, retrieve, forget; reindex the PC file index | `memory_search`, `memory_write`, `memory_delete`, `memory_forget_all`, `reindex_folder` | T0/T1/T2; T3 delete; T4 forget all |
| Tool Builder | Generate, test (Windows Sandbox, no network) and submit read-only tools | `find_existing_tools`, `scaffold_tool`, `run_tool_tests`, `submit_for_approval` | T2; new tools quarantined until the owner approves |
| Communication (daily-life) | Gmail (official API) search, read, draft, send; WhatsApp Desktop (UI Automation) list, read, send | `search_messages`, `read_message`, `draft_message`, `send_message`, `whatsapp_list_chats`, `whatsapp_read_chat`, `whatsapp_send` | T1 read; T2 draft; T3 send, each approved. Outlook blocked (university admin consent) |
| Planner (daily-life) | Reminders, to-dos, deadlines; tray notifications; local only | `planner_add`, `planner_list`, `planner_close` | T1 list; T2 add/close |
| Documents & Study (daily-life) | Read and summarize documents, quizzes, flashcards, new Word files | `read_document`, `create_document`, `search_files` | T1 read; T2 create in `workspace/documents/` |
| System Settings (owner decision 2026-11-19) | Reversible Windows toggles: Wi-Fi, Bluetooth, volume/mute, brightness, dark/light mode; status (incl. battery saver) | `get_settings_status` (T1), `set_radio`, `set_volume`, `set_brightness`, `set_theme` | T3 (approval every change) |
| Home / Device (later) | Not built in v1.0 | — | — |

Notes:

- Tool names are HCWMA tool contracts, not guaranteed capabilities of any service. Each must be backed by a real supported action.
- Image Editing in v1 means deterministic edits through libraries (Pillow, OpenCV, ImageMagick). Understanding image content depends on the chosen Qwen checkpoint being multimodal. Generative editing (inpainting, background replacement) needs a separate image model and is an expansion item. *(v1.2: Pillow only; no OpenCV/ImageMagick, no retouch, no generative editing.)*
- New agents are added by writing a capability card and a tool allowlist; no change to the Router code.

### 4.4 Agent capability card [NEW]

Each agent is defined by one file in `agents/cards/`.

```yaml
id: file_system
name: File & System Agent
status: enabled            # enabled | disabled
description: >
  Finds, organizes and recovers files on this PC. Handles "I lost a file",
  "where is X", "free up disk space", "what changed recently".
handles:                   # routing hints for the Router
  - missing or lost file
  - find file by name, type, date or content
  - disk usage, large files, duplicates
does_not_handle:
  - editing file contents (→ coding / documents)
tools: [search_files, list_recent_files, check_recycle_bin, restore_file, get_disk_usage]
permission_tier: T1
approval_required: [restore_file, move_file, delete_file]
memory_scopes: [pc_index, episodic, task]
system_prompt: prompts/file_system.md
limits: { max_steps: 15, timeout_s: 120 }
```

### 4.5 Task envelope (Router ↔ sub-agent contract) [NEW]

```json
{
  "task_id": "uuid",
  "parent_task_id": null,
  "from_agent": "router",
  "to_agent": "file_system",
  "goal": "Find the user's missing project file 'annek-proposal'",
  "context": { "user_message": "...", "memory_refs": ["..."] },
  "constraints": { "max_steps": 15, "depth": 1, "read_only": true },
  "result": {
    "status": "done | needs_approval | needs_clarification | failed",
    "summary": "...",
    "artifacts": [],
    "tool_calls": []
  }
}
```

### 4.6 Worked example: "I'm missing one of my project files on my laptop" [NEW]

1. **Router** reads the message, matches "missing or lost file" → File & System agent. Delegates with `read_only: true`.
2. **File & System agent** asks memory which projects the user has and where they usually live.
3. It runs `search_files` against the PC index (name, extension, recent dates), then `list_recent_files`, then `check_recycle_bin`, then checks cloud-sync folders if any are in approved folders.
4. It returns candidates: path, last-modified date, and where each was found.
5. If the file is in the Recycle Bin, `restore_file` is a write action → the app shows an approval prompt → on approval, restore.
6. If the file is truly gone, the agent says so and lists remaining options (backups, cloud version history, git history). It does not invent a result.
7. **Router** replies to the user; the outcome is written to episodic memory and the audit log.

---

## 5. Qwen Model, Source Code and Customization

### 5.1 What is downloaded [UNCHANGED]

- **Model implementation/source:** Python classes and architecture code in a framework such as Hugging Face Transformers.
- **Model weights:** trained numerical parameters, commonly distributed as `.safetensors` files.
- **Configuration:** model dimensions, layer types, attention settings, generation defaults and related metadata.
- **Tokenizer and processor assets:** vocabulary, tokenization rules, chat template, and image/video processing configuration where applicable.
- **License and model card:** usage conditions, limitations, intended use and attribution requirements.

### 5.2 Inspecting the code in VS Code [UNCHANGED]

Use the official Transformers repository and inspect the Qwen 3.5 model implementation directory. Exact file names and layout can change between releases; check the current repository before editing.

```bash
git clone https://github.com/huggingface/transformers.git
cd transformers
code .
# Inspect: src/transformers/models/qwen3_5/
```

Keep a clean upstream copy and make HCWMA changes in a separate fork or branch. Record the upstream commit, Python/PyTorch/Transformers versions, model revision and any local patches. Do not edit generated files blindly; follow the model directory's contribution and generation conventions.

### 5.3 What should be customized? [CHANGED — one row added]

| Change | Recommended approach |
|--------|----------------------|
| New abilities such as browsing, email, calendar, files or device control | Build tools/adapters and expose them through MCP; do not change model weights. |
| **A new kind of task (new "worker" in the factory)** | **Add a sub-agent: capability card + system prompt + tool allowlist. No model change.** |
| Personal style, response format or domain behaviour | System instructions, prompts, memory and evaluation; consider fine-tuning only if needed. |
| Domain knowledge | RAG, curated documents and citations; fine-tuning is not a substitute for a live knowledge base. |
| Model architecture / attention / layers | Research-only branch, unit tests, compatibility checks and potentially additional training. Existing pretrained weights may no longer fit. |
| Language or task performance | Evaluate first; then consider curated fine-tuning data, adapters (e.g. LoRA) and regression testing. |

> **Important:** model source code and model weights are different. Editing Python architecture code does not automatically retrain the model or change its learned parameters.

### 5.4 Model selection and runtime [CHANGED]

Begin with a checkpoint that fits the PC's actual GPU/CPU memory and target latency. The prior discussion considered smaller Qwen 3.5 checkpoints (around 4B where an official compatible checkpoint is available) and a 9B-class option. Verify the exact current official checkpoint name, license, architecture support and hardware requirements before downloading. Do not assume every size is available in every Qwen generation. *(v1.2 as built: Ollama `qwen3.5:4b`, Q4_K_M, CPU only on an i5-1135G7 / 16 GB / Iris Xe, thinking off, context 8192.)*

Because the model now shares a PC with the user's normal work, favour a **quantized checkpoint** through a serving engine such as llama.cpp or Ollama for day-to-day running, when the exact architecture is supported. Keep Hugging Face Transformers + PyTorch for research and source inspection. Benchmark the chosen runtime with the selected checkpoint before committing.

### 5.5 One model, many agents [NEW]

- One model instance is loaded. Agents call it through a single local inference endpoint with a request queue.
- An agent is: system prompt + capability card + tool allowlist + memory scopes. Switching agents means switching prompt and tools, not weights.
- Agents run one at a time by default (sequential). Parallel agents only if benchmarking shows the PC can handle it.
- The Router uses short prompts and low output limits so routing stays fast.
- Optional later: a second, smaller model just for routing, if measurements justify it. *(v1.2: not built; a card-defined fast path routes clear requests with no model call instead.)*

---

## 6. Universal Application Connector: API, MCP, Automation and Self-Built Tools

This remains a core HCWMA feature. The goal is to connect real-world applications even when they publish no official API or MCP server. The correct method depends on what interfaces the application exposes and what its owner permits.

### 6.1 The integration routes [CHANGED — PC access added]

| Route | When to use it | Key limitation |
|-------|----------------|----------------|
| Official API | The service publishes a supported API and authentication method. | API scopes, quotas, plan limits and endpoint coverage apply. |
| Custom API adapter | The service has a documented or otherwise authorized interface HCWMA can call. | A wrapper does not create new access rights or server-side capabilities. |
| Browser automation | A user can perform the operation through a website UI and automation is permitted. | UI changes, MFA, CAPTCHA, anti-bot controls and terms may limit automation. |
| Desktop / local integration | The application exposes accessibility controls, CLI hooks, local files, plugins or supported databases. | Requires OS-level permissions and careful handling of local data. |
| **Direct PC access** | **The task is about the PC itself: files, folders, processes, installed apps, shell, screen.** | **Governed by the permission tiers in 9.4; protected paths stay off-limits.** |

### 6.2 Critical distinction: API vs MCP [UNCHANGED]

- An API is an interface through which software requests data or actions from a service.
- MCP is a standard protocol through which an AI host discovers and invokes tools, resources and prompts exposed by an MCP server.
- An MCP server can call an official API, a custom adapter, Playwright or a local service.
- Creating an MCP server does not grant access to a remote application's private backend. The adapter still needs a legitimate way to perform the operation.

### 6.3 Application integration decision tree [CHANGED — Tool Builder step added]

```
Need a capability
   ↓
Does an approved tool already exist in the registry?
   ├── YES → use it
   └── NO
        ↓
Is there an official API / OAuth integration?
   ├── YES → build an official API adapter
   └── NO
        ↓
Is there a documented / authorized endpoint, plugin, CLI or local interface?
   ├── YES → build a custom adapter
   └── NO
        ↓
Can the user do the task through an accessible UI, and is automation permitted?
   ├── YES → Playwright (web) or accessibility/UI automation (desktop)
   └── NO  → mark unsupported; request vendor integration or use a permitted alternative
        ↓
Who builds it?  Tool Builder agent (6.6) or a developer
        ↓
Expose approved operations as MCP tools and enforce permissions
```

### 6.4 Central MCP design [CHANGED — runs locally, `generated/` added]

Use one central integration registry and one or more **local** MCP servers grouped by trust boundary. Prefer stdio transport or localhost-only binding. Each application receives its own adapter module. Agents see a consistent tool catalogue filtered by their allowlist.

```
integrations/
  registry/
    app_registry.py
    capability_catalog.py
  mcp/
    server.py
    tool_schemas.py
  adapters/
    pc/                # files, processes, shell, screen
    official_api/
    custom_api/
    browser/
    desktop/
    local_data/
  generated/           # tools written by the Tool Builder agent
    <tool_name>/
      manifest.yaml
      adapter.py
      tests/
  auth/
    oauth_manager.py
    session_vault.py
security/
  permission_manager.py
  approval_service.py
  audit_logger.py
```

### 6.5 Example tool design [CHANGED — PC tools added]

```python
# PC
search_files(query: str, roots: list[str] | None = None, limit: int = 50)
list_recent_files(days: int = 7)
check_recycle_bin(query: str)
restore_file(item_id: str)                 # approval required
run_in_sandbox(command: str, cwd: str)     # workspace only

# Messages
search_messages(query: str, limit: int = 20)
read_message(message_id: str)
draft_message(to: str, subject: str, body: str)
send_message(draft_id: str)                # approval required

# Web
open_website(url: str)
extract_page_information(url: str)
```

These are example HCWMA tool contracts, not guaranteed capabilities of any particular service. Each tool must be backed by a real supported action and must declare its permissions, input validation, data handling and approval requirement.

### 6.6 Self-built APIs and MCP tools (Tool Builder agent) [NEW]

When an agent needs a capability that is not in the registry, the Tool Builder can create it.

Lifecycle:

```
1. NEED        Agent or Router reports a missing capability.
2. CHECK       Tool Builder searches the registry for an existing or similar tool.
3. ROUTE       It applies the decision tree in 6.3 to pick a legitimate route.
4. GENERATE    It writes manifest.yaml, adapter.py and tests into integrations/generated/<tool>/.
5. TEST        Tests run in a sandbox: no secrets, no network unless the manifest declares it. (v1.2: Windows Sandbox, network off)
6. QUARANTINE  Tool is registered with status "quarantined". No agent can call it yet.
7. REVIEW      Owner sees a plain summary: what it does, what it touches, which permissions it asks for.
8. APPROVE     On approval, status becomes "approved" and it joins the MCP catalogue.
9. MAINTAIN    Approved by content hash; state in %LOCALAPPDATA%\HCWMA\generated_tools; any edit re-quarantines; can be disabled or rolled back.
```

Manifest:

```yaml
name: get_weather_forecast
version: 0.1.0
created_by: tool_builder
status: quarantined          # quarantined | approved | disabled
route: official_api          # official_api | custom_api | browser | desktop | pc
description: Fetch a 3-day forecast for a city.
inputs: { city: string }
outputs: { forecast: list }
permissions:
  network: ["api.example-weather.com"]
  filesystem: []
  secrets: []
side_effects: none           # none | writes | external_action
approval_required: false
```

Hard rules:

- A generated tool cannot grant itself permissions. Everything it may touch is declared in the manifest and approved by the owner.
- The Tool Builder can write only inside `integrations/generated/` and its sandbox.
- A generated tool cannot modify the policy layer, the audit log, the agent registry or other tools.
- Generated tools follow the same non-goals as hand-written ones: no bypassing logins, MFA, CAPTCHA or service terms.
- Changing an approved tool's permissions sends it back to quarantine.

---

## 7. Voice, Language and Multi-Device Access

### 7.1 Voice pipeline [CHANGED — Router replaces controller]

```
Microphone on the PC
   ↓
Push-to-talk (hold F9 or the talk button in the window; no wake word in v1.0)
   ↓
STT (speech-to-text)
   ↓
Language and intent detection
   ↓
Router mini agent → sub-agent(s) + Qwen
   ↓
Response text
   ↓
TTS (text-to-speech)
   ↓
Speaker / headphones
```

- Keep STT and TTS replaceable; evaluate language coverage before choosing a model.
- Test English, Tamil, Sinhala, code-switching and transliterated forms separately.
- Do not assume a speech model supports every language or accent equally well.
- Voice identity checks, if used, are an additional security signal, not the only authentication factor for sensitive actions.
- Start with push-to-talk or explicit activation; add wake-word behaviour after privacy and false-activation testing. *(v1.2: push-to-talk only.)*
- **New:** STT/TTS also use the PC's RAM. *(v1.2: faster-whisper small, CPU int8, loaded only while talking; needs 1340 MB free, else Qwen is unloaded for the transcription and reloaded. TTS: MMS for English and Tamil, espeak-ng for Sinhala. English reliable; Tamil and Sinhala experimental.)*

### 7.2 Multi-device access [CHANGED — now optional, later]

- v1.0 is single-PC only (multi-device was not built). The desktop app is the primary interface.
- All services bind to `127.0.0.1` by default.
- Later, optionally, the PC can act as the host for a phone or second device:
  - LAN access only at first, turned on explicitly by the owner.
  - VPN for remote access rather than exposing ports to the public internet.
  - Register devices and users; allow the owner to revoke access.
  - Encrypted transport, secure sessions and per-device permissions.
- If always-on access becomes important, the dedicated-server plan from v1.0 remains a valid upgrade; the architecture does not change.

---

## 8. Persistent Memory and Knowledge [CHANGED]

The model's context window is temporary working context, not a reliable long-term database. HCWMA memory is an independent service, now **shared by every agent**. This is what gives the factory its "big memory": the knowledge lives in the memory service and is retrieved when needed, not stored inside the model.

| Memory layer | Purpose | Who uses it |
|--------------|---------|-------------|
| Conversation summaries | Compact history and active task continuity. | Router, all agents |
| Profile / preferences | User-approved stable preferences and settings. | All agents |
| Episodic memory | Important past events, decisions and outcomes. | All agents |
| Knowledge base / RAG | Search personal documents and approved reference material. | All agents | *(v1.2: not built.)*
| Structured state | Tasks, permissions, integration state and workflow status. | Router, policy layer |
| **Agent knowledge cards** | **What each agent can do, its tools and limits.** | **Router** |
| **Agent skill notes** | **Lessons an agent learned ("this project uses pnpm", "fix for error X").** | **The owning agent** |
| **PC file index** | **Names, paths, types, sizes, dates (and optionally content) of files in approved folders.** | **File & System, others via it** | *(v1.2: names, sizes, dates only; no content, owner decision.)*
| **Tool catalogue** | **Every tool, its manifest, status and usage stats.** | **Router, Tool Builder** |
| **Task scratchpad** | **Working notes for the current task; cleared when it ends.** | **Active agent** |

Rules:

- Each agent reads only the memory scopes listed on its capability card.
- The Memory (Librarian) agent is the only agent that deletes or restructures long-term memory.
- The PC file index covers only owner-approved folders and skips protected paths (9.4).

Start with SQLite for configuration, structured records and the file index (SQLite FTS for text search). Add a vector database such as Qdrant when semantic retrieval volume and requirements justify it. Define retention, deletion, export, backup and consent rules before storing sensitive information. *(v1.2: memory is SQLite FTS5 in `%LOCALAPPDATA%\HCWMA`; no Qdrant.)*

---

## 9. Security, Privacy and Safety

Giving an AI access to the whole PC raises the stakes compared with v1.0, so this section is stricter, not looser.

### 9.1 Mandatory controls [CHANGED]

- Access through a per-install API token kept in Windows Credential Manager and handed to the window in memory; no separate owner account or unlock in v1.0.
- Role- and scope-based access control for every agent and integration.
- OAuth or the application's supported authentication method; never collect passwords unnecessarily.
- Store API keys, refresh tokens and browser session state in the OS credential store or an encrypted vault; never commit them to Git.
- Use MFA where the service supports it. Never attempt to bypass MFA or CAPTCHA.
- Require confirmation for irreversible or high-impact actions.
- Read-only access by default; request write scopes only when a feature requires them.
- Validate tool arguments, restrict URL destinations where appropriate, and defend against prompt injection in web pages, documents **and files on the PC**.
- Keep audit logs while minimizing sensitive content in logs.
- Rate-limit tools and isolate browser sessions and untrusted content.
- Back up data securely and test restoration.
- **New:** Run HCWMA as a normal user, never as administrator/root. Elevation is a separate, explicit, per-action approval.
- **New:** Agent-written code runs only in the sandbox workspace until the owner moves it out.
- **New:** A visible "stop everything" control in the app and tray that halts all agents immediately.

### 9.2 Browser session warning [UNCHANGED]

Playwright authentication state can contain cookies and headers that may impersonate an account. Store it outside source control, restrict filesystem permissions, encrypt backups and provide a way to revoke sessions.

### 9.3 Approval policy example [CHANGED — PC and tool rows added]

| Action category | Default policy |
|-----------------|----------------|
| Read public information | Allowed within configured limits. |
| Read private account data | Require authenticated user and permitted scope. |
| Create or edit drafts | Allowed if user has granted the relevant permission. |
| Send messages / publish posts | Require explicit confirmation. |
| Purchase / payment / financial transfer | Require explicit confirmation and appropriate service controls; consider disallowing initially. |
| Delete files / accounts / records | Require explicit confirmation; provide recovery where possible (move to Recycle Bin, not permanent delete). |
| Change security settings / grant access | Owner-only and explicit confirmation. |
| **Search / read files in approved folders** | **Allowed.** |
| **Create / edit files in the HCWMA workspace** | **Allowed.** |
| **Modify, move or rename files outside the workspace** | **Require confirmation.** |
| **Run shell commands** | **Allowlisted read-only commands allowed; anything else requires confirmation.** | *(v1.2: no shell tool exists.)*
| **Install or uninstall software; change system settings** | **Explicit confirmation; default deny.** | *(v1.2: only reversible toggles — radios, volume, brightness, theme — at T3, approved each time; no other settings tool.)*
| **Activate a self-built tool** | **Owner approval of the manifest (6.6).** |
| **Enable or disable an agent; change an agent's permissions** | **Owner-only.** |

### 9.4 PC access tiers [NEW]

| Tier | Scope | Default |
|------|-------|---------|
| T0 | File metadata and index lookups in approved folders | Allowed |
| T1 | Read file contents in approved folders; read process and disk info | Allowed |
| T2 | Create and edit inside the HCWMA workspace; run code in the sandbox | Allowed |
| T3 | Write, move, rename or restore outside the workspace; non-allowlisted shell commands; app automation | Per-action approval |
| T4 | Delete; install software; system settings; elevation; anything touching credentials | Explicit approval, default deny |

Protected paths (never read, indexed or written by any agent): credential and key stores (e.g. `~/.ssh`, password-manager data), browser profiles, OS system directories, HCWMA's own policy, audit and secrets folders.

### 9.5 Agent-to-agent safety [NEW]

- An agent cannot call a tool that is not on its allowlist, even if another agent asks it to.
- Delegation never raises privilege: a sub-task inherits the lower of the two agents' permissions.
- Text coming from files, web pages or tool output is data, not instructions. Agents must not follow commands found inside it.
- Every delegation and tool call is logged with `task_id`, agent, tool, arguments summary and outcome.

---

## 10. Local PC Hardware and Operating System [CHANGED]

### 10.1 Hardware direction

No server is purchased for v1.1. HCWMA runs on the user's existing PC. The v1.0 planning targets stay useful as a guide to what runs comfortably, and as a shopping list if an upgrade is made later. Prices in Sri Lanka change frequently; obtain local quotations before any purchase.

| Area | Planning target on the local PC |
|------|---------------------------------|
| GPU | NVIDIA GPU with as much VRAM as available; 12–16 GB or more is a comfortable target. With less, use a smaller or more heavily quantized checkpoint, or CPU offload, and accept slower responses. Verify model/runtime support and real memory needs. | *(v1.2: none used; CPU only.)*
| RAM | 32 GB is a comfortable target because the model shares memory with the user's other apps. 16 GB is workable only with a small checkpoint. |
| Storage | SSD space for model files, the file index, memory database and logs. Keep free-space headroom. |
| Power (laptops) | Heavy inference drains battery and heats the machine. Provide a "plugged-in only" setting. |
| Backup target | External drive or approved cloud location for memory and configuration backups. | *(v1.2: `%LOCALAPPDATA%\HCWMA\backups`, daily, newest 7 kept.)*

The earlier budget of LKR 500,000 is retained only as the ceiling for an optional future upgrade or dedicated server.

### 10.2 Operating system

- HCWMA runs on the PC's existing desktop OS (assumed Windows; see A2).
- Keep the code cross-platform: use `pathlib`, avoid hard-coded paths, isolate OS-specific code behind adapter interfaces (`adapters/pc/windows.py`, `linux.py`, `macos.py`). *(v1.2: only Windows adapters exist.)*
- On Windows, WSL2 is an option for the model/runtime if a dependency needs Linux; the PC-access adapters must still run natively.
- If a dedicated server is added later, the v1.0 guidance applies: evaluate Ubuntu Server 26.04 LTS first, with 24.04 LTS as fallback if a critical dependency is not yet compatible.

### 10.3 Local network security

- Bind the model endpoint, backend API and MCP servers to `127.0.0.1` only.
- Do not expose model, MCP, database or admin ports to the LAN or the public internet.
- Protect the local API with a per-install token so other local programs cannot drive HCWMA silently.
- Leave the OS firewall on; add no inbound rules for HCWMA in v1.1.
- Keep the OS, GPU drivers and dependencies patched.

---

## 11. Recommended Software Stack [CHANGED]

| Layer | Initial recommendation | Notes |
|-------|------------------------|-------|
| Language | Python | Primary backend, agents, integrations and ML. |
| Model development | PyTorch + Hugging Face Transformers | Best for inspecting and experimenting with model implementation. |
| Model serving | **Ollama `qwen3.5-text:4b` (`qwen3.5:4b` Q4_K_M weights, no vision projector) on CPU (as built)**; HCWMA starts `ollama serve` itself if it is down | Transformers kept for research only. |
| Backend API | FastAPI **on localhost** | Authenticated local HCWMA services. |
| **Desktop app shell** | **PySide6 window (Qt WebEngine) + tray icon (as built)** | **Approval prompts, stop control, start with Windows.** |
| Agent orchestration | A small custom Python **Router + agent runtime** | Keep policy and tool execution explicit; avoid heavy framework coupling. |
| **Agent definitions** | **YAML capability cards + Markdown system prompts** | **Add an agent without touching Router code.** |
| Tool protocol | Official MCP Python SDK | Check current SDK major version and migration notes before pinning. |
| **PC access** | **`pathlib`, `psutil`, `watchdog`, `send2trash`; OS accessibility APIs for UI control** | **Wrapped as MCP tools under the tier policy.** |
| **Code sandbox** | **Guarded runner in `workspace/code/` for Coding/Debugging; Windows Sandbox (no network) for Tool Builder tests** | **As built.** |
| **Image tools** | **Pillow** | **Deterministic edits for the Image Editing agent.** |
| Browser / app automation | Web: httpx + HTML parser (read-only). Desktop: Windows UI Automation (incl. WhatsApp Desktop). No Playwright. | Browsers and password fields are refused. |
| Structured storage | SQLite (with FTS for the file index); PostgreSQL later if needed | One source of truth for application state. |
| Vector retrieval | Qdrant when needed | Do not add before a real retrieval requirement exists. | *(v1.2: not added.)*
| STT / TTS | faster-whisper small (CPU int8); MMS-TTS (en, ta) via sherpa-onnx; espeak-ng (si) | Chosen on the owner's own recordings (docs/voice_benchmarks_*.md). |
| **Secrets** | **OS credential store via `keyring`** | **No secrets in files or Git.** |
| Deployment | **`install.cmd`/`install.ps1`, one-click `start_hcwma.cmd`, tray app, Start with Windows shortcut (as built)** | Docker not used. |
| Monitoring | Structured logs + basic health endpoints | Add dashboards after core services work. |

Pin tested versions in a lockfile or requirements file. Do not install arbitrary latest versions into the working install without a test environment.

---

## 12. Proposed Repository / Folder Structure [CHANGED]

```
HCWMA/
  docs/
  configs/
    permissions.yaml          # tiers, approved folders, protected paths
    model.yaml
  model/
    source/                   # upstream/fork reference; keep patches documented
    serving/
    evaluation/
  backend/
    api/
    auth/
    schemas/
  app/                        # desktop shell (UI, tray, approval prompts)
  agents/
    router/
      router.py
      classifier.py
    runtime/
      agent_runtime.py        # runs any agent from its card
      task_envelope.py
      model_queue.py          # single shared model, queued requests
      policy.py
    registry/
      agent_registry.py
    cards/                    # one YAML per agent
      file_system.yaml
      coding.yaml
      debugging.yaml
      image_editing.yaml
      web_research.yaml
      app_automation.yaml
      memory_librarian.yaml
      tool_builder.yaml
      communication.yaml
      planner.yaml
      documents_study.yaml
    prompts/                  # one system prompt per agent
  memory/
    storage/
    retrieval/
    summarization/
    pc_index/
  voice/
    stt/
    tts/
    activation/
  integrations/
    registry/
    mcp/
    adapters/
      pc/
      official_api/
      custom_api/
      browser/
      desktop/
      local_data/
    generated/                # Tool Builder output
    auth/
  devices/
  security/
    permissions/
    approvals/
    secrets/
    audit/
  workspace/                  # sandbox for agent-written code and files (gitignored)
  tests/
  deployment/
    installer/
    scripts/
  .env.example
  .gitignore
  README.md
```

Keep model weights, private datasets, API secrets, browser profiles, logs containing private data, the PC file index and personal documents out of Git.

---

## 13. Step-by-Step Development Roadmap [CHANGED]

Phases are renumbered because the server phase is gone and the agent phases are new.

| Phase | Work | Completion test |
|-------|------|-----------------|
| 0. Planning | Record the PC's specs; confirm model checkpoint, threat model, approved folders and data policy. | Written spec sheet and approved architecture. |
| 1. Local PC foundation | Update OS and GPU drivers; create the HCWMA workspace and data folders; set up backups. | GPU detected; folders and backup target ready. |
| 2. Development environment | Python environment, Git repository, pinned dependencies and tests. | Reproducible setup from a clean checkout. |
| 3. Qwen baseline | Download a verified checkpoint; run local inference; try a quantized runtime. | Repeatable benchmark with memory and latency recorded on this PC. |
| 4. Source exploration | Read Qwen config/model implementation; fork/branch and document patches. | Small tested change with regression tests; upstream recoverable. |
| 5. Backend + desktop app | FastAPI on localhost, owner auth, chat endpoint, desktop shell with tray. | Authenticated local chat works in the app. |
| 6. Tool foundation | Tool schemas, central registry, local MCP server/client, permission checks, audit log. | A test MCP tool is discovered and invoked safely. |
| 7. Agent runtime + Router | Capability cards, agent registry, task envelope, shared model queue, Router. | Router delegates a test task to a dummy agent and returns its result. |
| 8. First sub-agent: File & System | PC file index over approved folders; read-only search tools. | "Find my missing file" works end to end and is logged. |
| 9. Approval workflow | Confirmation prompts for T3/T4 actions; stop control. | Unapproved sensitive action is blocked; stop halts all agents. |
| 10. Persistent memory | Summaries, structured memory, retrieval, agent skill notes, retention and deletion. | Information is stored, retrieved by the right agents, and removed correctly. |
| 11. Coding + Debugging agents | Sandbox workspace, run/test tools, patch application. | Agent writes a small program and fixes a seeded bug inside the sandbox. |
| 12. Image Editing + Web/Research + App Automation agents | Image library tools; Playwright; UI automation. | Each agent completes one scripted task within its permissions. |
| 13. Tool Builder | Generation, sandbox tests, quarantine, review and approval flow. | Agent builds one simple tool; it is unusable until approved, usable after. |
| 14. Voice | STT and TTS; test language and accent coverage. | End-to-end voice round trip meets agreed quality. |
| 15. Daily-life agents | Communication, Planner, Documents & Study; first real application integration (prefer official API). | Read-only task succeeds; send/write actions require approval. |
| 16. Multi-device access (optional) | LAN/VPN access, device registration. | Only authorized devices can connect. |
| 17. Home/device integrations | Selected smart devices through supported interfaces. | Each action respects permissions. |
| 18. Reliability | Backups, restore drills, updates, health checks, crash recovery, resource limits. | App recovers from restart and restores data; PC stays usable under load. |
| 19. Expansion | Optimize serving, more agents and tools, fine-tune if justified, generative image editing, dedicated server if needed. | Measured improvement without regressions. |

### Recommended first milestone

Do not build every agent at once. The first complete milestone is phases 0–8:

**Local PC + one Qwen checkpoint + FastAPI on localhost + Router + File & System agent with read-only search + permission checks + logs.**

This proves the factory pattern (Router → sub-agent → tool → result) with one real, useful task before adding more agents.

---

## 14. Testing and Acceptance Criteria [CHANGED]

- **Model:** verify model revision, license, tokenizer, supported inputs, GPU memory use, latency and repeatability on the actual PC.
- **Router:** a fixed set of sample requests routes to the expected agent; ambiguous requests trigger clarification; unknown requests do not get guessed.
- **Agents:** each agent refuses tools outside its allowlist; step budget and depth limit stop runaway tasks; tool arguments are schema-validated.
- **MCP:** test discovery, tool invocation, error handling and timeouts; confirm nothing listens beyond localhost.
- **PC access:** protected paths cannot be read or indexed; T3/T4 actions are blocked without approval; delete goes to Recycle Bin.
- **Tool Builder:** a quarantined tool cannot be called; a tool cannot exceed its manifest; a permission change returns it to quarantine.
- **Integrations:** test read-only access first; test expired credentials, denied scopes, UI changes and rate limits.
- **Security:** prompt-injection test files and web pages do not cause actions; stop control works mid-task.
- **Memory:** verify retrieval accuracy, scope isolation between agents, deletion, retention and backup/restore.
- **Voice:** measure word error rate or task success on representative English/Tamil/Sinhala samples.
- **Reliability:** test app crash, PC sleep/wake, low disk, low battery and GPU busy with another program.
- **Regression:** rerun a fixed test suite after changing model, prompts, agent cards, adapters or dependencies.

### Definition of done for each integration [UNCHANGED]

- Documented purpose, supported operations and limitations.
- Identified owner/account and approved authentication method.
- Explicit read/write scopes and approval rules.
- Input/output schemas and error handling.
- Tests for success, permission denial, timeout and expired session.
- Audit logging and secret-handling review.
- User-facing description of what HCWMA can and cannot do.

### Definition of done for each agent [NEW]

- Capability card with `handles` / `does_not_handle`, tools, tier and limits.
- System prompt reviewed and stored in `agents/prompts/`.
- Routing test cases: at least 10 that should reach this agent, 5 that should not.
- One end-to-end scripted task passing.
- Refusal test: asks for a tool outside its allowlist and is denied.
- Entry in the user-facing list of what HCWMA can do.

---

## 15. Risks and Mitigations [CHANGED]

| Risk | Mitigation |
|------|------------|
| Model too large for available VRAM | Smaller checkpoint, quantization or CPU offload; benchmark on the real PC first. |
| Qwen architecture changes break local patches | Keep a fork, pin commits and maintain tests; rebase deliberately. |
| Third-party site blocks automation or changes UI | Prefer official APIs; isolate Playwright adapters and fail gracefully. |
| Session/token theft | OS credential store, file permissions, encryption, revocation, no secrets in Git/logs. |
| Prompt injection from web content **or local files** | Treat external and file content as untrusted data; tool allowlists and policy checks. |
| Accidental external action | Read-only defaults, confirmation gates and audit logs. |
| Power/network interruptions | Save task state; resume or cancel cleanly on restart. |
| Scope creep | Deliver one vertical slice (one agent) at a time with acceptance tests. |
| **Router sends a task to the wrong agent** | Clear `handles` / `does_not_handle` on cards; routing test set; clarify on low confidence. |
| **Agents loop or bounce a task between each other** | Step budget, depth limit, timeouts, stop control. |
| **Agent damages files or the system** | Tiers, workspace sandbox, protected paths, Recycle Bin instead of delete, non-admin process. |
| **Self-built tool is buggy or unsafe** | Sandbox tests, quarantine, manifest review, owner approval, easy disable/rollback. |
| **HCWMA slows the PC during normal work** | Quantized model, pause/unload control, sequential agents, plugged-in-only option. |
| **A small local model is not smart enough for hard coding/debugging tasks** | Narrow each agent's job, give it good tools and tests, measure, and set honest expectations in the UI. |
| **Assistant unavailable when the PC is off** | Accepted for v1.1; dedicated server remains an optional upgrade. |

---

## 16. Project Decisions and Open Items

### Decisions carried forward [CHANGED]

- Project name: HCWMA.
- Primary model direction: open-source Qwen rather than training a 100B+ model from scratch.
- Model research/customization stack: PyTorch + Hugging Face Transformers; source inspected in VS Code.
- **Changed:** runs on the user's local PC as a desktop application. No dedicated server in v1.1.
- **Changed:** the LKR 500,000 budget and server hardware baseline apply only to an optional future upgrade.
- **New:** agent-factory design: Router mini agent + specialist sub-agents defined by capability cards.
- **New:** one shared local Qwen model for all agents.
- **New:** Tool Builder agent may create API adapters and MCP tools, gated by owner approval.
- **New:** whole-PC access under tiers T0–T4 with protected paths.
- Backend direction: Python + FastAPI (localhost); central MCP/tool gateway and application-specific adapters.
- Memory direction: persistent storage outside the model context window, shared by all agents.
- Access direction: the owner first; other people/devices only if multi-device access is enabled later.
- All project files and model assets organized and archived on local storage.

### Items to confirm during implementation [CHANGED]

- Exact Qwen checkpoint and license after checking current official model cards.
- **The PC's actual specs (GPU/VRAM, RAM, free disk) and which checkpoint size they support.**
- **Target operating system (assumption A2: Windows).**
- **Meaning of "agents for what a human needs" (assumption A1: daily-life agents) and which ones come first.**
- **Whether self-built tools need owner approval before first use (assumption A3: yes).**
- **Which folders are approved for indexing and which are off-limits.**
- **Desktop shell choice (Tauri / Electron / PySide).**
- **Whether image editing needs generative features, which require a separate image model.**
- First application to integrate and whether it permits API, browser or desktop automation.
- Exact supported languages and voice quality target.
- Data retention and backup encryption policy.
- Whether phone/second-device access is wanted at all in this version.

---

## 17. Official References and Documentation [CHANGED — carried from v1.0, PC-access links added]

Use these primary sources when implementing. Check the latest release notes and compatibility matrix at installation time; online documentation and model repositories can change. The links below are carried over from v1.0 or added by name; they were not re-checked for this revision.

- Qwen 3.5 Transformers documentation: https://huggingface.co/docs/transformers/main/model_doc/qwen3_5
- Hugging Face Transformers Qwen 3.5 source directory: https://github.com/huggingface/transformers/tree/main/src/transformers/models/qwen3_5
- Qwen official model organization: https://huggingface.co/Qwen
- Official MCP Python SDK: https://github.com/modelcontextprotocol/python-sdk
- MCP Python SDK documentation: https://py.sdk.modelcontextprotocol.io/
- Playwright Python documentation: https://playwright.dev/python/
- Playwright authentication guidance: https://playwright.dev/python/docs/auth
- FastAPI documentation: https://fastapi.tiangolo.com/
- PyTorch documentation: https://pytorch.org/docs/stable/index.html
- Qdrant documentation: https://qdrant.tech/documentation/
- llama.cpp: https://github.com/ggml-org/llama.cpp
- Ollama documentation: https://docs.ollama.com/
- Tauri documentation: https://tauri.app/
- psutil documentation: https://psutil.readthedocs.io/
- Ubuntu release lifecycle (only if a dedicated server is added later): https://ubuntu.com/about/release-cycle

Current-state note (from v1.0): Qwen 3.5 Transformers documentation describes a multimodal model family and a hybrid attention design. Confirm the exact checkpoint variant and required dependency versions before installing. Pin a tested MCP SDK version rather than relying on an unpinned install.

---

## 18. Final Recommendation [CHANGED]

Build HCWMA as a secure, modular **local agent factory** on the user's own PC, not as a heavily modified monolithic model and not as a separate server. Start with a working local Qwen inference service, then add the Router, the agent runtime, the local MCP gateway, one sub-agent and the approval/security controls. Add memory, more agents, the Tool Builder, voice and daily-life agents in later phases.

> **The most valuable first proof-of-concept:** the user says "I'm missing one of my project files" → the Router picks the File & System agent → HCWMA checks permission → an MCP tool searches the PC index, recent files and Recycle Bin → the agent reports what it found with an audit record → restoring the file asks for approval first.

Once that one path works end to end, every further capability is the same pattern with a new capability card and a new set of tools.
