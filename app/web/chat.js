// HCWMA chat page (build step 5).
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

const history = []; // [{role, content}] — the conversation so far, kept in this tab only

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
  (locked ? tokenInput : chatInput).focus();
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

async function send(text) {
  history.push({ role: "user", content: text });
  addMessage("user", text);
  const pending = addMessage("assistant pending", "Qwen is thinking…");
  sendButton.disabled = true;
  try {
    const response = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": "Bearer " + memoryToken },
      body: JSON.stringify({ messages: history }),
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
      body.completion_tokens + " tokens · " + body.tokens_per_s + " tok/s · " + body.total_s + " s",
    );
  } catch {
    pending.remove();
    history.pop();
    addMessage("error", "Could not reach the backend. Is it still running?");
  } finally {
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

showLocked(!memoryToken);
checkHealth();
