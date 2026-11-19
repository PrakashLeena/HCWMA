// The health page (build step 18). Read-only: it only shows what /health/details reports.
// Every value is shown with textContent, never as HTML.
"use strict";

const TOKEN_KEY = "hcwma-token";
const el = (id) => document.getElementById(id);

function token() {
  try { return sessionStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
}

function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

function mark(ok) {
  if (ok === true) return ["OK", "mark ok"];
  if (ok === false) return ["!", "mark bad"];
  return ["?", "mark unknown"];
}

function banner(id, text) {
  const node = el(id);
  node.textContent = text || "";
  node.hidden = !text;
}

async function load() {
  const status = el("status");
  if (!token()) {
    status.textContent = "Open the chat page first so it can unlock this one.";
    return;
  }
  status.textContent = "checking… (Gmail and WhatsApp can take a few seconds)";
  status.className = "status";
  let report;
  try {
    const response = await fetch("/health/details", { headers: { "Authorization": "Bearer " + token() } });
    if (!response.ok) throw new Error("failed (" + response.status + ")");
    report = await response.json();
  } catch (err) {
    status.textContent = "Could not reach HCWMA: " + err.message;
    status.className = "status bad";
    return;
  }
  const body = el("checks").tBodies[0];
  body.replaceChildren();
  for (const item of report.items) {
    const [symbol, className] = mark(item.ok);
    const row = document.createElement("tr");
    row.append(cell(symbol, className), cell(item.name, "name"), cell(item.value));
    body.append(row);
  }
  const notes = [...report.warnings];
  banner("refused", report.heavy_tasks_refused || (notes.length ? notes.join(" ") : ""));
  banner("recovery", report.recovery);

  const settings = el("settings");
  settings.replaceChildren();
  const lines = [
    "Model: " + (report.model_paused ? "paused (Resume model in the tray)" : "running when needed"),
    "Heavy tasks only when plugged in: " + (report.settings.heavy_only_when_plugged_in ? "on" : "off"),
    "Start with Windows: " + (report.start_with_windows === null ? "unknown" : (report.start_with_windows ? "on" : "off")),
    "New requests refused below " + (report.settings.min_free_ram_mb / 1024).toFixed(0) + " GB free RAM or "
      + report.settings.min_free_disk_gb.toFixed(0) + " GB free disk",
  ];
  for (const line of lines) {
    const li = document.createElement("li");
    li.textContent = line;
    settings.append(li);
  }
  status.textContent = "checked " + report.checked_at.replace("T", " ");
}

el("refresh").addEventListener("click", load);
load();
