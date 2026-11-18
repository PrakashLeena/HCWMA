// Owner review of tools written by the Tool Builder (build step 13).
// Shows the plain summary, the declared permissions AND the full code. Everything the tool
// or the Tool Builder wrote is shown with textContent only, never as HTML.
"use strict";

const TOKEN_KEY = "hcwma-token";
const el = (id) => document.getElementById(id);
const listEl = el("list");
const detailEl = el("detail");
const statusEl = el("status");

function token() {
  try { return sessionStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", "Authorization": "Bearer " + token() },
  });
  let body = {};
  try { body = await response.json(); } catch { /* no body */ }
  if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "failed (" + response.status + ")");
  return body;
}

function node(tag, text, className) {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (className) n.className = className;
  return n;
}

async function loadList() {
  listEl.replaceChildren();
  if (!token()) {
    statusEl.textContent = "Open the chat page first so it can unlock this one.";
    return;
  }
  try {
    const { tools } = await api("/generated-tools");
    statusEl.textContent = tools.length ? tools.length + " tool(s)" : "No tools written yet.";
    for (const t of tools) {
      const row = node("div", undefined, "tool-row");
      row.append(node("span", t.name, "name"), node("span", t.status, "badge " + t.status),
        node("span", t.reason, "status"));
      row.addEventListener("click", () => showTool(t.name));
      listEl.append(row);
    }
  } catch (error) {
    statusEl.textContent = error.message;
  }
}

function section(title, child) {
  detailEl.append(node("h2", title), child);
}

async function showTool(name) {
  const t = await api("/generated-tools/" + encodeURIComponent(name));
  detailEl.hidden = false;
  detailEl.replaceChildren(node("h2", t.name + " — " + t.status));
  detailEl.append(node("p", t.reason));
  section("What it does (written by HCWMA from the manifest)", node("div", t.summary, "summary"));
  const perms = Object.entries(t.permissions).map(([k, v]) => k + ": " + (Array.isArray(v) ? (v.join(", ") || "none") : v));
  section("Permissions it asks for", node("pre", perms.join("\n")));
  section("Manifest", node("pre", t.manifest_yaml));
  section("Full code (adapter.py)", node("pre", t.adapter_code));
  section("Tests (test_adapter.py)", node("pre", t.test_code));
  const checks = [];
  checks.push("Static check: " + (t.problems.length ? "FAILED" : "passed"));
  for (const p of t.problems) checks.push("  - " + p);
  checks.push("Sandbox tests for these exact files: " + (t.tested ? (t.tested.passed ? "passed" : "FAILED") + " (" + t.tested.summary + ")" : "not run"));
  checks.push("Submitted by the Tool Builder: " + (t.submitted ? "yes" : "no"));
  checks.push("Files fingerprint: " + t.hash);
  section("Checks", node("pre", checks.join("\n")));

  const actions = node("div", undefined, "actions");
  const message = node("span", "", "warn");
  const boxes = [];
  if (t.status !== "approved") {
    actions.append(node("span", "Agents that may use it:"));
    for (const id of t.agents_available) {
      const label = node("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = id;
      boxes.push(box);
      label.append(box, document.createTextNode(id));
      actions.append(label);
    }
    const approve = node("button", "Approve these exact files");
    approve.disabled = !(t.tested && t.tested.passed && t.submitted && !t.problems.length);
    approve.addEventListener("click", () => decide(name, "approve",
      { hash: t.hash, agents: boxes.filter((b) => b.checked).map((b) => b.value) }, message));
    actions.append(approve);
  } else {
    actions.append(node("span", "Allowed for: " + t.agents_granted.join(", ")));
  }
  if (t.status === "disabled") {
    const enable = node("button", "Switch back on");
    enable.addEventListener("click", () => decide(name, "enable", undefined, message));
    actions.append(enable);
  } else {
    const disable = node("button", "Disable", "danger");
    disable.addEventListener("click", () => decide(name, "disable", undefined, message));
    actions.append(disable);
  }
  for (const v of t.previous_versions) {
    const back = node("button", "Roll back to version " + v.version);
    back.addEventListener("click", () => decide(name, "rollback", { hash: v.hash }, message));
    actions.append(back);
  }
  actions.append(message);
  detailEl.append(actions);
}

async function decide(name, action, body, message) {
  try {
    await api("/generated-tools/" + encodeURIComponent(name) + "/" + action,
      { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
    await loadList();
    await showTool(name);
  } catch (error) {
    message.textContent = error.message;
  }
}

loadList();
