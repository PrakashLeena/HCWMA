# HCWMA Backlog (after v1.0)

Every open item from `docs/PROGRESS.md` notes, `docs/real-use-notes.md`, `docs/CAPABILITIES.md`,
`docs/DECISIONS.md` and the v1.0 smoke test, merged and ordered by importance
(safety and wrong answers first, then speed, then missing features). 2026-11-20.

## 1. Must fix (wrong or unsafe results)

Fixed for v1.0 (2026-11-20), kept here for the record:
- ~~Web/Research tools quarantined, so web answers were invented~~ → owner approved the manifests;
  the smoke test read example.com for real.
- ~~Agents could answer with 0 tool calls~~ → cards with `requires_tool` get "I could not do
  that: <reason>" instead (runtime guard, tested).
- ~~The Router stated live facts itself (it "told" the time)~~ → `live_facts` on the cards send
  such questions to the agent with the tool, or the Router says it cannot check (tested).

1. **Ollama's `llama-server` grows to ~5.7 GB RSS** (Ollama reports 3.2 GB for the model), so free
   RAM falls under HCWMA's 1 GB floor and new requests are refused. Pause/Resume model frees it.
   Find out why (KV cache, vision projector, keep-alive) and whether `num_ctx` or the floor should change.
   **Done 2026-11-21 (branch `step-19-model-ram`, `docs/ram_soak.md`):** not a leak. ~5.0 GB private
   right after loading (all weights private because Ollama loads without mmap, +0.9 GB vision projector),
   +~0.5 GB saved prompt states, then flat at ~5.5 GB over 20 requests. Fixes: bounded prompt cache /
   checkpoints and one slot on the Ollama HCWMA starts, `num_ctx` on the load call too, and an idle RAM
   guard that unloads the model after 5 idle minutes above 5.3 GB private. Text-only daily model
   (2026-11-21, `docs/text_model.md`): another ~1.0 GB less.
2. **Debugging agent with inline code**: for "print(1/0) crashes, what is wrong?" it searched
   `workspace/code/` instead of explaining, answered about an unrelated file twice and once used
   all 10 steps (smoke test 2026-11-20). Quality issue, not tuned for v1.0.
3. **Tool Builder says "I will now build a new tool"** after being told not to (it did not build
   one in the smoke test). Wording issue in its prompt.
4. **Web: DNS-rebinding gap** (the address check and the connection are separate lookups).
   Pin the checked IP for the request. Web/Research also has no search-engine tool and cannot
   read JavaScript-only pages: it can only read a link the owner gives.
## 2. Speed (CPU only)

5. Requests take 40 s – 3 min: Router ~40 s when the fast path does not match; "find amma
   folder" 74 s (was 193 s), Word-file search 135 s, web 93–196 s, Gmail send 81 s, settings
   changes 75–165 s, coding demo ~8 min. Ollama does not reuse the prompt prefix on this
   model. Ideas: more fast-path rules, shorter agent prompts, smaller model for routing,
   code-built answers after one successful tool call (done for File & System).
6. After Stop, the abandoned Ollama call still runs to the end and blocks the next request.

## 3. Routing and multi-step requests

7. "Is there a tool that counts the words in a text file?" goes to File & System (the word
   "file"); "what time is it in Tokyo?" gives this PC's time. Six older known routing failures (owner chose not to tune, 2026-11-18): "today's date" and
   "free space on C" answered by the Router (now caught by `live_facts`); two image edge
   cases; three near misses go to clarify or tool_builder.
8. Full routing eval not re-run since App Automation, Image, Gmail, WhatsApp, Planner,
   Documents and System Settings were added. Re-run `python -m agents.router.eval_routing`.
9. Cross-agent chains fail: "read this site and save it as a text file" (web → file),
   "summarize X and save as Word" splits badly.
10. A pending clarification question is forgotten after a restart.
11. Requests that fit no agent are not offered to the Tool Builder (Router says it cannot).

## 4. Voice

12. Sinhala not recorded or measured (Step 14 stays unticked):
    `python -m voice.bench.record_samples --lang si` then `python -m voice.bench.stt_bench`.
13. Tamil is weak (44% character errors); mixed Tamil+English comes back as English.
    Benchmark a Tamil fine-tuned Whisper small on the owner's clips.
14. Push-to-talk works only while the HCWMA window is focused (no global hotkey).
15. The first ~0.3 s after "speak now" is silence while the mic warms up; MMS logs a warning
    for each punctuation character it skips (noisy console).

## 5. App and account integrations

16. App Automation cannot open programs and does not know installed app names ("open
    antigravity" → clarification; "open WhatsApp" → refused).
17. Outlook is blocked by university admin consent. Options: a personal Microsoft account,
    or ask IT for consent. No SMS/Telegram.
18. Gmail OAuth app in "Testing" mode: the login expires every 7 days.
19. WhatsApp: chat list is virtualised (only visible chats), the window must be in front,
    emoji outside the BMP can fail the exact-text check (then nothing is sent).
20. Tool Builder: HTML only (no JSON APIs), no tray notice for tools waiting for review,
    owner still to confirm its design choices (DECISIONS).

## 6. Feature gaps

21. File & System: no content search; the file-index watcher is not restarted when the
    approved folder list changes; no approving from the tray; no folder or multi-file moves.
22. Memory: word match only (no semantic search); backups not encrypted; restore by command line only.
23. Coding sandbox is a guarded runner, not a container; no `git_status`.
24. Planner: no repeating reminders, no online calendar. Documents: no scanned PDFs,
    spreadsheets or slides.
25. System Settings: no battery saver, Do Not Disturb or external-monitor brightness.
26. Not built: Step 16 multi-device, Step 17 home devices, Step 19 expansion; Step 4 source
    exploration (optional).

## 7. Reliability and housekeeping

27. Ollama auto-start is tested with mocks only; try it live once (quit Ollama, start HCWMA).
28. PC sleep tested by simulation only; low battery level is not a limit; owner to confirm
    the Step 18 choices.
29. `docs/real-use-checklist.md`: every item and the speed log still unticked.
30. `docs/DECISIONS.md` disk numbers are stale (C: now ~43 GB free after moving Ollama models to E:).
31. Starlette's test client deprecates `httpx` in favour of `httpx2`.
32. `CLAUDE.md` ends with a few stray pasted lines after "Keep this section current".
