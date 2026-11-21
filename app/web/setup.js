// First-run wizard (2026-11-21): PC check, app scan and selection, adapters, approved folders.
// Every value is shown with textContent, never as HTML. Only clicks here change anything.
"use strict";

const TOKEN_KEY = "hcwma-token";
const el = (id) => document.getElementById(id);
let step = 0;
let setup = null;
let apps = null;
const chosen = new Set(); // app ids ticked in step 2

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

async function post(path, body) {
  const r = await fetch(path, { method: "POST", headers: headers(), body: JSON.stringify(body || {}) });
  if (!r.ok) {
    let detail = "failed (" + r.status + ")";
    try { detail = (await r.json()).detail || detail; } catch { /* keep the status */ }
    el("status").textContent = detail;
  }
  return r;
}

function show(n) {
  step = n;
  for (const s of document.querySelectorAll("section[data-step]")) s.hidden = Number(s.dataset.step) !== n;
  for (const s of document.querySelectorAll("#steps span")) s.classList.toggle("on", Number(s.dataset.step) === n);
  el("back").disabled = n === 0;
  el("next").textContent = n === 3 ? "Finish" : "Next";
}

function renderPc() {
  const body = el("pc").tBodies[0];
  body.replaceChildren();
  const items = setup.pc ? setup.pc.items : [];
  if (!items.length) body.appendChild(cell("The PC check is not available in this mode.", "small"));
  for (const item of items) {
    const tr = document.createElement("tr");
    const mark = item.ok === true ? ["OK", "mark ok"] : item.ok === false ? ["!", "mark bad"] : ["?", "mark unknown"];
    tr.append(cell(mark[0], mark[1]), cell(item.name, "name"), cell(item.value));
    body.appendChild(tr);
  }
}

function renderApps() {
  const body = el("apps").tBodies[0];
  body.replaceChildren();
  const words = el("filter").value.trim().toLowerCase();
  const list = apps ? apps.apps.filter((a) => !a.blocked) : [];
  if (!list.length) {
    body.appendChild(cell(apps && apps.scanning ? "Scanning…" : "No apps found yet: press Scan again.", "small"));
    return;
  }
  for (const a of list) {
    if (words && !a.name.toLowerCase().includes(words)) continue;
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.className = "act";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = a.allowed || chosen.has(a.id);
    box.disabled = a.allowed;
    box.setAttribute("aria-label", "HCWMA may open " + a.name);
    box.addEventListener("change", () => { if (box.checked) chosen.add(a.id); else chosen.delete(a.id); });
    td.appendChild(box);
    tr.append(td, cell(a.name, "name"), cell(a.allowed ? "already allowed" : (a.publisher || a.kind), "small"));
    body.appendChild(tr);
  }
}

function renderAdapters() {
  const body = el("adapters").tBodies[0];
  body.replaceChildren();
  for (const a of apps.adapters) {
    const tr = document.createElement("tr");
    let note = a.needs_app ? (a.found.length ? a.found.join(", ") + " found." : "App not found on this PC.") : "";
    if (a.id === "gmail" && setup.gmail) note = "Login: " + setup.gmail.text + ".";
    note += " " + a.login_hint;
    const td = document.createElement("td");
    td.className = "act";
    const b = document.createElement("button");
    b.type = "button";
    const on = a.state === "on";
    b.textContent = on ? "Turn off" : "Turn on";
    if (on) b.className = "quiet";
    b.disabled = a.needs_app && !a.found.length && !on;
    b.addEventListener("click", async () => {
      b.disabled = true;
      await post("/adapters/" + a.id, { on: !on, source: "wizard" });
      await loadApps();
      renderAdapters();
    });
    td.appendChild(b);
    tr.append(cell(a.name, "name"), cell(on ? "on" : "off"), cell(note.trim(), "small"), td);
    body.appendChild(tr);
  }
}

function renderFolders() {
  const body = el("folders").tBodies[0];
  body.replaceChildren();
  for (const f of setup.folders) {
    const tr = document.createElement("tr");
    const where = f.path + (f.onedrive ? " (OneDrive)" : "") + (f.exists ? "" : " (missing)");
    let action;
    if (f.approved) action = cell("approved", "small");
    else if (!f.exists) action = cell("", "small");
    else {
      action = document.createElement("td");
      action.className = "act";
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = "Approve";
      b.addEventListener("click", async () => {
        b.disabled = true;
        const r = await post("/setup/folders", { path: f.path });
        if (r.ok) { f.approved = true; el("status").textContent = "Added; used after HCWMA restarts."; }
        renderFolders();
      });
      action.appendChild(b);
    }
    tr.append(cell(f.name, "name"), cell(where), action);
    body.appendChild(tr);
  }
}

async function loadApps() {
  const r = await fetch("/apps/state", { headers: headers() });
  if (r.ok) apps = await r.json();
}

async function load() {
  if (!token()) { el("status").textContent = "Open the chat page first so it can unlock this one."; return; }
  el("status").textContent = "checking this PC…";
  const r = await fetch("/setup/state", { headers: headers() });
  if (!r.ok) { el("status").textContent = "could not load (" + r.status + ")"; return; }
  setup = await r.json();
  await loadApps();
  el("status").textContent = setup.done ? "setup was finished before; you can change things here" : "first-run setup";
  renderPc();
  renderApps();
  renderAdapters();
  renderFolders();
  show(0);
}

async function saveChosen() {
  for (const id of chosen) await post("/apps/" + encodeURIComponent(id) + "/allow", { source: "wizard" });
  chosen.clear();
  await loadApps();
}

el("back").addEventListener("click", () => show(Math.max(0, step - 1)));
el("next").addEventListener("click", async () => {
  if (step === 1) { await saveChosen(); renderAdapters(); }
  if (step < 3) { show(step + 1); return; }
  await post("/setup/finish", { how: "finished" });
  window.location.href = "/";
});
el("skip").addEventListener("click", async () => {
  await post("/setup/finish", { how: "skipped" });
  window.location.href = "/";
});
el("scan").addEventListener("click", async () => {
  el("scan").disabled = true;
  el("status").textContent = "scanning… (about 10 s)";
  await post("/apps/scan");
  await loadApps();
  el("scan").disabled = false;
  el("status").textContent = "scan done";
  renderApps();
  renderAdapters();
});
el("filter").addEventListener("input", renderApps);
load();
