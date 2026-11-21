// The Apps page (2026-11-21): which apps HCWMA may open without asking (revoke), adapters
// (on / off), and every app the scan found. Every value is shown with textContent, never HTML.
// Only clicks on this page (with the per-install token) change anything.
"use strict";

const TOKEN_KEY = "hcwma-token";
const el = (id) => document.getElementById(id);
let state = null;

function token() {
  try { return sessionStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
}

function headers() {
  return { "Content-Type": "application/json", "Authorization": "Bearer " + token() };
}

function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

function button(text, onClick, quiet) {
  const td = document.createElement("td");
  td.className = "act";
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = text;
  if (quiet) b.className = "quiet";
  b.addEventListener("click", async () => { b.disabled = true; await onClick(); await load(); });
  td.appendChild(b);
  return td;
}

async function post(path, body) {
  const r = await fetch(path, { method: "POST", headers: headers(), body: JSON.stringify(body || {}) });
  if (!r.ok) {
    let detail = "failed (" + r.status + ")";
    try { detail = (await r.json()).detail || detail; } catch { /* keep the status */ }
    el("status").textContent = detail;
  }
  return r;
}

function renderAllowed() {
  const body = el("allowed").tBodies[0];
  body.replaceChildren();
  if (!state.allowed.length) {
    const tr = document.createElement("tr");
    tr.appendChild(cell("None yet. HCWMA asks you each time it wants to open an app.", "small"));
    body.appendChild(tr);
  }
  for (const a of state.allowed) {
    const tr = document.createElement("tr");
    tr.append(cell(a.name, "name"), cell("since " + a.since + " (" + a.source + ")", "small"),
      button("Revoke", () => post("/apps/" + encodeURIComponent(a.id) + "/revoke")));
    body.appendChild(tr);
  }
  el("listed").textContent = state.owner_listed.length
    ? "Also opened without asking, from configs/apps.yaml (your overrides): " + state.owner_listed.join(", ")
    : "";
}

function renderAdapters() {
  const body = el("adapters").tBodies[0];
  body.replaceChildren();
  for (const a of state.adapters) {
    const tr = document.createElement("tr");
    const found = a.needs_app
      ? (a.found.length ? "found: " + a.found.join(", ") : "app not found on this PC")
      : "no app needed";
    const label = a.state === "on" ? "on" : (a.state === "off" ? "off" : "not decided (off)");
    tr.append(cell(a.name, "name"), cell(label), cell(found + ". " + a.login_hint, "small"),
      a.state === "on"
        ? button("Turn off", () => post("/adapters/" + a.id, { on: false }), true)
        : button("Turn on", () => post("/adapters/" + a.id, { on: true })));
    body.appendChild(tr);
  }
}

function renderApps() {
  const body = el("apps").tBodies[0];
  body.replaceChildren();
  const words = el("filter").value.trim().toLowerCase();
  const showBlocked = el("show-blocked").checked;
  let shown = 0;
  for (const a of state.apps) {
    if (a.blocked && !showBlocked) continue;
    if (words && !a.name.toLowerCase().includes(words)) continue;
    const tr = document.createElement("tr");
    if (a.blocked) tr.className = "blocked";
    const notes = [a.kind === "store" ? "Store app" : "desktop app"];
    if (a.publisher) notes.push(a.publisher);
    if (a.adapter) notes.push("adapter: " + a.adapter);
    let action;
    if (a.blocked) action = cell("never opened: " + a.blocked, "small");
    else if (a.allowed) action = cell("always allowed", "small");
    else action = button("Always allow", () => post("/apps/" + encodeURIComponent(a.id) + "/allow", { source: "settings" }), true);
    tr.append(cell(a.name, "name"), cell(notes.join(" · "), "small"), action);
    body.appendChild(tr);
    shown += 1;
  }
  if (!shown) {
    const tr = document.createElement("tr");
    tr.appendChild(cell(state.apps.length ? "No app matches." : "No scan yet: press Scan again.", "small"));
    body.appendChild(tr);
  }
}

async function load() {
  if (!token()) { el("status").textContent = "Open the chat page first so it can unlock this one."; return; }
  const r = await fetch("/apps/state", { headers: headers() });
  if (!r.ok) { el("status").textContent = "could not load (" + r.status + ")"; return; }
  state = await r.json();
  const blocked = state.apps.filter((a) => a.blocked).length;
  el("status").textContent = state.scanning ? "scanning…"
    : (state.scanned_at ? state.apps.length + " apps (" + blocked + " blocked), scanned " + state.scanned_at : "not scanned yet");
  renderAllowed();
  renderAdapters();
  renderApps();
}

el("scan").addEventListener("click", async () => {
  el("status").textContent = "scanning… (about 10 s)";
  el("scan").disabled = true;
  await post("/apps/scan");
  el("scan").disabled = false;
  await load();
});
el("filter").addEventListener("input", () => state && renderApps());
el("show-blocked").addEventListener("change", () => state && renderApps());
load();
