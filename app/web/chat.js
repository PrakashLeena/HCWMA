// HCWMA chat page (build step 5; approval prompts and Stop added in step 9).
// The API token is kept in sessionStorage: it is gone when the tab closes and is never
// written to disk by this page. Model replies are shown with textContent, never as HTML.
"use strict";

const TOKEN_KEY = "hcwma-token";
const el = (id) => document.getElementById(id);
const messagesEl = el("messages");
const statusEl = el("status");
const tokenForm = el("token-form");
const tokenInput = el("token-input");
const chatForm = el("chat-form");
const chatInput = el("chat-input");
const sendButton = el("send");
const forgetButton = el("forget");
const stopButton = el("stop");
const approvalsEl = el("approvals");
const voiceRow = el("voice-row");
const talkButton = el("talk");
const voiceLang = el("voice-lang");
const voiceState = el("voice-state");

const history = []; // [{role, content}] — the conversation so far, kept in this tab only
// One id per chat (page load). HCWMA keeps one short summary per chat under it (step 10).
const sessionId = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + "-" +
  Math.random().toString(16).slice(2, 10));

function getToken() {
  try { return sessionStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
}

function setToken(value) {
  try {
    if (value) sessionStorage.setItem(TOKEN_KEY, value);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch { /* storage blocked: the token then lasts for this page load only */ }
  memoryToken = value;
}

let memoryToken = getToken();

function showLocked(locked) {
  tokenForm.hidden = !locked;
  chatForm.hidden = locked;
  forgetButton.hidden = locked;
  stopButton.hidden = locked;
  if (locked) renderApprovals([]);
  (locked ? tokenInput : chatInput).focus();
}

// ---- build step 9: approval prompts and the stop control ----
// A tool call that needs the owner pauses on the backend. This page asks every second
// whether something is waiting and shows it with Approve / Deny. Only these buttons (and
// the token behind them) can answer; nothing typed into the chat can.

function authHeaders() {
  return { "Content-Type": "application/json", "Authorization": "Bearer " + memoryToken };
}

function renderApprovals(pending) {
  const wanted = new Set(pending.map((p) => p.id));
  for (const card of Array.from(approvalsEl.children)) {
    if (!wanted.has(card.dataset.id)) card.remove();
  }
  for (const item of pending) {
    let card = approvalsEl.querySelector('[data-id="' + item.id + '"]');
    if (!card) {
      card = buildApprovalCard(item);
      approvalsEl.appendChild(card);
    }
    card.querySelector(".who").textContent =
      item.agent + " wants to use " + item.tool + " (tier " + item.tier + ") · " +
      "waits " + item.expires_in_s + " s more, then counts as not approved";
  }
  approvalsEl.hidden = pending.length === 0;
}

function buildApprovalCard(item) {
  const card = document.createElement("div");
  card.className = "approval";
  card.dataset.id = item.id;
  card.setAttribute("role", "alertdialog");
  const title = document.createElement("h2");
  title.textContent = "HCWMA needs your approval";
  const what = document.createElement("p");
  what.className = "what";
  what.textContent = item.summary; // text only, never HTML
  const who = document.createElement("p");
  who.className = "who";
  const row = document.createElement("div");
  row.className = "row";
  const deny = document.createElement("button");
  deny.type = "button";
  deny.className = "quiet";
  deny.textContent = "Deny";
  const approve = document.createElement("button");
  approve.type = "button";
  approve.textContent = "Approve once";
  deny.addEventListener("click", () => decide(item.id, "deny", card));
  approve.addEventListener("click", () => decide(item.id, "approve", card));
  row.append(deny, approve);
  card.append(title, what, who, row);
  return card;
}

async function decide(id, decision, card) {
  for (const button of card.querySelectorAll("button")) button.disabled = true;
  try {
    const response = await fetch("/approvals/" + encodeURIComponent(id), {
      method: "POST", headers: authHeaders(), body: JSON.stringify({ decision }),
    });
    if (!response.ok) addMessage("error", "That request was no longer waiting for an answer.");
  } catch {
    addMessage("error", "Could not reach the backend.");
  }
  pollApprovals();
}

async function pollApprovals() {
  if (!memoryToken) return;
  try {
    const response = await fetch("/approvals", { headers: authHeaders() });
    if (response.ok) renderApprovals((await response.json()).pending);
  } catch { /* the health line already shows when the backend is gone */ }
}

async function stopEverything() {
  stopButton.disabled = true;
  try {
    const response = await fetch("/stop", { method: "POST", headers: authHeaders() });
    if (response.ok) {
      const body = await response.json();
      // A halted request reports "Stopped" itself, in its own reply.
      if (!body.stopped_tasks) addMessage("error", "Nothing was running.");
    } else {
      addMessage("error", "Stop failed (" + response.status + ").");
    }
  } catch {
    addMessage("error", "Could not reach the backend to stop it.");
  } finally {
    stopButton.disabled = false;
    pollApprovals();
  }
}

function addMessage(kind, text, meta) {
  const div = document.createElement("div");
  div.className = "msg " + kind;
  div.textContent = text;
  if (meta) {
    const small = document.createElement("span");
    small.className = "meta";
    small.textContent = meta;
    div.appendChild(small);
  }
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div;
}

async function checkHealth() {
  try {
    const response = await fetch("/health");
    if (!response.ok) throw new Error();
    statusEl.textContent = "backend running on this PC";
    statusEl.classList.remove("bad");
  } catch {
    statusEl.textContent = "backend not reachable";
    statusEl.classList.add("bad");
  }
}

// `voice` is set for a push-to-talk request ({language, meta}). The transcript is sent exactly
// like typed text; only the reply is also spoken.
async function send(text, voice) {
  history.push({ role: "user", content: text });
  addMessage(voice ? "user voice" : "user", text, voice ? voice.meta : undefined);
  const pending = addMessage("assistant pending", "HCWMA is working…");
  sendButton.disabled = true;
  // While this request waits, ask whether HCWMA is starting Ollama, and say so.
  const watch = setInterval(async () => {
    try {
      const r = await fetch("/model/state", { headers: authHeaders() });
      if (!r.ok) return;
      const body = await r.json();
      pending.textContent = body.state === "starting"
        ? "Starting the local model… (this can take up to a minute)"
        : (body.activity || "HCWMA is working…"); // e.g. "Opening WhatsApp…" (fixed text)
    } catch { /* the request itself reports a lost backend */ }
  }, 1000);
  try {
    const response = await fetch("/chat", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ messages: history, session_id: sessionId }),
    });
    pending.remove();
    if (response.status === 401) {
      history.pop();
      setToken("");
      showLocked(true);
      addMessage("error", "The token was not accepted. Paste it again.");
      return;
    }
    if (!response.ok) {
      history.pop();
      let detail = "Request failed (" + response.status + ").";
      try {
        const body = await response.json();
        if (typeof body.detail === "string") detail = body.detail;
      } catch { /* keep the generic message */ }
      addMessage("error", detail);
      return;
    }
    const body = await response.json();
    history.push({ role: "assistant", content: body.reply });
    addMessage(
      "assistant",
      body.reply,
      (body.agents.length ? "router → " + body.agents.join(" → ") : "router") +
        " · " + body.status + (body.stopped_by ? " (stopped: " + body.stopped_by + ")" : "") +
        " · " + body.tool_calls + " tool calls · " + body.total_s + " s",
    );
    if (voice) speak(body.reply);
  } catch {
    pending.remove();
    history.pop();
    addMessage("error", "Could not reach the backend. Is it still running?");
  } finally {
    clearInterval(watch);
    sendButton.disabled = false;
    chatInput.focus();
  }
}

tokenForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const value = tokenInput.value.trim();
  if (!value) return;
  setToken(value);
  tokenInput.value = "";
  showLocked(false);
  initVoice();
});

chatForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = chatInput.value.trim();
  if (!text || sendButton.disabled) return;
  chatInput.value = "";
  send(text);
});

chatInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    chatForm.requestSubmit();
  }
});

forgetButton.addEventListener("click", () => {
  setToken("");
  showLocked(true);
});

stopButton.addEventListener("click", () => {
  resetTalk("");
  stopEverything();
});

// ---- build step 14: push-to-talk ----
// The backend opens the microphone only while the talk button or F9 is held, keeps the audio
// in memory and returns what it heard. The transcript is shown and sent like typed text, so
// a voice request meets the same approval cards. Saying "yes" approves nothing: only the
// Approve button on a card can.

const LANG_KEY = "hcwma-voice-lang";
let held = false; // the owner is holding the button or F9
let opening = null; // the /voice/start request, while it is in flight
let listening = false; // the backend microphone is open

function resetTalk(state) {
  held = false;
  listening = false;
  talkButton.classList.remove("listening");
  talkButton.textContent = "Hold to talk (F9)";
  voiceState.textContent = state;
}

async function voicePost(path, body) {
  const response = await fetch(path, {
    method: "POST", headers: authHeaders(), body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await response.json(); } catch { /* empty body */ }
  if (!response.ok) {
    throw new Error(data && typeof data.detail === "string" ? data.detail : "voice request failed");
  }
  return data;
}

async function startTalk() {
  if (held || opening || listening || sendButton.disabled || talkButton.disabled) return;
  held = true;
  // The microphone takes a moment to open: say "speak now" only once it really listens,
  // so the first words are not lost.
  talkButton.textContent = "Opening microphone…";
  voiceState.textContent = "";
  opening = voicePost("/voice/start", { language: voiceLang.value });
  try {
    await opening;
    listening = true;
    talkButton.classList.add("listening");
    talkButton.textContent = "Listening — speak now, release to send";
  } catch (err) {
    resetTalk("");
    addMessage("error", "Voice: " + err.message);
    return;
  } finally {
    opening = null;
  }
  if (!held) finishTalk(); // released while the microphone was opening
}

async function finishTalk() {
  held = false;
  if (opening || !listening) return; // startTalk() calls back once the microphone is open
  listening = false;
  talkButton.classList.remove("listening");
  talkButton.classList.remove("listening");
  talkButton.textContent = "Hold to talk (F9)";
  voiceState.textContent = "Working out what you said…";
  talkButton.disabled = true;
  try {
    const heard = await voicePost("/voice/stop");
    voiceState.textContent = "";
    if (!heard.text) {
      addMessage("error", "I did not catch anything. Hold the button (or F9) while you speak.");
      return;
    }
    const name = voiceLang.options[voiceLang.selectedIndex].text.replace(" (experimental)", "");
    send(heard.text, {
      language: heard.language,
      meta: "heard (" + name + ") · " + heard.audio_seconds + " s recorded · level " +
        Math.round(heard.peak * 100) + "%" + (heard.peak < 0.03 ? " (very quiet)" : "") +
        " · " + heard.transcribe_seconds + " s to transcribe" +
        (heard.qwen_unloaded ? " · model unloaded to make room: +" +
          (heard.qwen_unload_s + heard.qwen_reload_s).toFixed(1) + " s (unload " +
          heard.qwen_unload_s + " s, reload " + heard.qwen_reload_s + " s)" : "") +
        (heard.debug_file ? " · debug copy " + heard.debug_file : ""),
    });
  } catch (err) {
    resetTalk("");
    addMessage("error", "Voice: " + err.message);
  } finally {
    talkButton.disabled = false;
  }
}

async function speak(text) {
  if (!text) return;
  try { await voicePost("/voice/speak", { text }); } catch { /* the reply is on screen */ }
}

async function initVoice() {
  try {
    const response = await fetch("/voice", { headers: authHeaders() });
    if (!response.ok) return; // no voice service: the row stays hidden
    const info = await response.json();
    voiceRow.hidden = false;
    talkButton.disabled = !info.available;
    if (!info.available) voiceState.textContent = info.reason;
  } catch { /* backend not reachable: the health line says so */ }
  try {
    const saved = localStorage.getItem(LANG_KEY);
    if (saved) voiceLang.value = saved;
  } catch { /* storage blocked: English */ }
}

voiceLang.addEventListener("change", () => {
  try { localStorage.setItem(LANG_KEY, voiceLang.value); } catch { /* not remembered */ }
});
talkButton.addEventListener("pointerdown", (event) => {
  event.preventDefault();
  talkButton.setPointerCapture(event.pointerId);
  startTalk();
});
talkButton.addEventListener("pointerup", finishTalk);
talkButton.addEventListener("pointercancel", finishTalk);
document.addEventListener("keydown", (event) => {
  if (event.key === "F9" && !event.repeat && !chatForm.hidden) {
    event.preventDefault();
    startTalk();
  }
});
document.addEventListener("keyup", (event) => {
  if (event.key === "F9") {
    event.preventDefault();
    finishTalk();
  }
});
window.addEventListener("blur", () => { if (held) finishTalk(); });

showLocked(!memoryToken);
if (memoryToken) initVoice();
checkHealth();
setInterval(pollApprovals, 1000);
