// The Model page (owner decisions 2026-11-26). Every value is shown with textContent, never
// HTML. Only clicks on this page (with the per-install token) change anything. Leaving Local
// only sends accept_warning only after the click on the warning's own button. An API key is
// sent once to the backend and the field is cleared at once; it is never kept in browser
// storage, and the backend only ever returns its last 4 characters.
"use strict";

const TOKEN_KEY = "hcwma-token";
const el = (id) => document.getElementById(id);
let state = null;
let accepted = false;

function token() {
  try { return sessionStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
}

function headers() {
  return { "Content-Type": "application/json", "Authorization": "Bearer " + token() };
}

async function call(method, path, body) {
  const r = await fetch(path, { method, headers: headers(), body: body ? JSON.stringify(body) : undefined });
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (!r.ok) {
    const detail = data && typeof data.detail === "string" ? data.detail : "failed (" + r.status + ")";
    throw new Error(detail);
  }
  return data;
}

function say(text) { el("result").textContent = text; }

function mode() {
  const picked = document.querySelector("input[name=mode]:checked");
  return picked ? picked.value : "local";
}

function profile(id) {
  return state.profiles.find((p) => p.id === id);
}

function button(text, onClick, quiet) {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = text;
  if (quiet) b.className = "quiet";
  b.addEventListener("click", onClick);
  return b;
}

function renderProfiles() {
  const rows = state.profiles.map((p) => {
    const tr = document.createElement("tr");
    const name = document.createElement("td");
    name.textContent = (p.active ? "● " : "") + p.label;
    const model = document.createElement("td");
    model.textContent = p.model || "(no model)";
    const key = document.createElement("td");
    key.textContent = p.key_saved ? "key ends with " + p.key_hint : (p.key_error || "no key");
    const acts = document.createElement("td");
    acts.className = "act";
    if (p.active) {
      const b = document.createElement("b");
      b.textContent = "in use";
      acts.appendChild(b);
    } else {
      acts.appendChild(button("Use this", () => activate(p.id)));
    }
    acts.appendChild(button("Edit", () => edit(p.id), true));
    acts.appendChild(button("Remove", () => removeProfile(p.id), true));
    tr.append(name, model, key, acts);
    return tr;
  });
  el("profiles").tBodies[0].replaceChildren(...rows);
  el("no-profiles").hidden = rows.length > 0;
}

function renderPresets() {
  if (el("preset").options.length) return;
  for (const [id, p] of Object.entries(state.presets)) {
    const o = document.createElement("option");
    o.value = id;
    o.textContent = p.label;
    el("preset").appendChild(o);
  }
}

// Fill the provider form from a saved profile, or from the preset if none is saved.
function edit(id) {
  el("preset").value = id;
  const p = profile(id);
  const preset = state.presets[id];
  el("base-url").value = p ? p.base_url : preset.base_url;
  el("base-url").readOnly = id !== "custom";
  el("model").value = p ? p.model : "";
  el("price-in").value = p ? p.price_input_per_mtok : 0;
  el("price-out").value = p ? p.price_output_per_mtok : 0;
  el("key-state").textContent = p && p.key_saved
    ? "Key saved (ends with " + p.key_hint + ")."
    : (p && p.key_error) || "No key saved for this provider.";
  el("model-list").hidden = true;
  el("model-list").replaceChildren();
  el("edit-title").textContent = (p ? "Change " : "Add ") + preset.label;
  if (p && p.key_saved) loadModels(true);
}

function render() {
  const s = state.settings;
  renderPresets();
  document.querySelectorAll("input[name=mode]").forEach((r) => { r.checked = r.value === s.mode; });
  renderProfiles();
  el("budget").value = s.monthly_budget_usd;
  const u = state.usage;
  const budget = state.budget_unlimited
    ? "no limit (free, unpriced)"
    : "US$" + Number(s.monthly_budget_usd).toFixed(2);
  el("usage").textContent = "This month: " + u.calls + " cloud calls, " +
    (u.prompt_tokens + u.completion_tokens).toLocaleString() + " tokens (" +
    u.prompt_tokens.toLocaleString() + " in, " + u.completion_tokens.toLocaleString() +
    " out), US$" + u.cost_usd.toFixed(4) + " of " + budget + "; " + u.fallbacks +
    " answered locally instead." + (state.budget_blocked ? " Budget used up: cloud calls stopped." : "");
  el("leaving").replaceChildren(...state.data_leaving.map((t) => {
    const li = document.createElement("li");
    li.textContent = t;
    return li;
  }));
  el("agents").tBodies[0].replaceChildren(...Object.entries(state.agents).map(([agent, where]) => {
    const tr = document.createElement("tr");
    const name = document.createElement("td");
    name.textContent = agent;
    const pick = document.createElement("td");
    const sel = document.createElement("select");
    sel.dataset.agent = agent;
    for (const v of ["local", "cloud"]) {
      const o = document.createElement("option");
      o.value = v;
      o.textContent = v === "cloud" ? "☁ cloud" : "🖥 local";
      sel.appendChild(o);
    }
    sel.value = where;
    pick.appendChild(sel);
    tr.append(name, pick);
    return tr;
  }));
  const active = profile(s.profile);
  el("status").textContent = s.mode === "local"
    ? "Local only"
    : (s.mode === "cloud" ? "Cloud" : "Hybrid") + " · " + (active ? active.label : "?") + " · " + s.model;
  el("warning").hidden = true;
}

async function load(editId) {
  try {
    state = await call("GET", "/cloud/status");
    render();
    edit(editId || state.settings.profile || "nvidia");
  } catch (e) {
    el("status").textContent = e.message;
  }
}

function profileBody(activate) {
  return {
    preset: el("preset").value,
    base_url: el("base-url").value.trim() || null,
    model: el("model").value.trim(),
    price_input_per_mtok: Number(el("price-in").value || 0),
    price_output_per_mtok: Number(el("price-out").value || 0),
    activate,
  };
}

async function saveProfile(activate) {
  state = await call("POST", "/cloud/profiles", profileBody(activate));
  render();
}

async function loadModels(quiet) {
  const id = el("preset").value;
  if (!quiet) say("Asking " + state.presets[id].label + " for its model list…");
  try {
    const r = await call("GET", "/cloud/profiles/" + encodeURIComponent(id) + "/models");
    const list = el("model-list");
    const current = el("model").value;
    const first = document.createElement("option");
    first.value = "";
    first.textContent = r.models.length + " models — pick one";
    list.replaceChildren(first, ...r.models.map((m) => {
      const o = document.createElement("option");
      o.value = m;
      o.textContent = m;
      return o;
    }));
    list.value = r.models.includes(current) ? current : "";
    list.hidden = r.models.length === 0;
    if (!quiet) say(r.models.length + " models found.");
  } catch (e) {
    if (!quiet) say("Model list: " + e.message);
  }
}

async function activate(id) {
  try {
    state = await call("POST", "/cloud/profiles/" + encodeURIComponent(id) + "/activate");
    render();
    edit(id);
    say("Now using " + profile(id).label + ".");
  } catch (e) { say(e.message); }
}

async function removeProfile(id) {
  try {
    state = await call("DELETE", "/cloud/profiles/" + encodeURIComponent(id));
    render();
    edit(state.settings.profile || "nvidia");
    say("Provider and its key removed.");
  } catch (e) { say(e.message); }
}

function overrides() {
  const out = {};
  document.querySelectorAll("#agents select").forEach((sel) => { out[sel.dataset.agent] = sel.value; });
  return out;
}

function needsWarning() {
  return mode() !== "local" &&
    (state.settings.mode === "local" || !state.settings.warning_accepted_at);
}

async function save() {
  if (needsWarning() && !accepted) {
    el("warning").hidden = false;
    el("warning").scrollIntoView({ behavior: "smooth" });
    say("Read the warning and click to turn the cloud model on.");
    return;
  }
  try {
    state = await call("POST", "/cloud/settings", {
      mode: mode(),
      agent_overrides: overrides(),
      monthly_budget_usd: Number(el("budget").value || 0),
      accept_warning: accepted,
    });
    render();
    say("Saved.");
  } catch (e) {
    say(e.message);
  } finally {
    accepted = false;
  }
}

el("save").addEventListener("click", save);
el("accept").addEventListener("click", () => { accepted = true; save(); });
el("cancel").addEventListener("click", () => {
  accepted = false;
  document.querySelector("input[name=mode][value=" + state.settings.mode + "]").checked = true;
  el("warning").hidden = true;
  say("Nothing was changed.");
});
document.querySelectorAll("input[name=mode]").forEach((r) => r.addEventListener("change", () => {
  accepted = false;
  el("warning").hidden = !needsWarning();
}));
el("preset").addEventListener("change", () => edit(el("preset").value));
el("model-list").addEventListener("change", () => {
  if (el("model-list").value) el("model").value = el("model-list").value;
});
el("load-models").addEventListener("click", () => loadModels(false));
el("save-profile").addEventListener("click", async () => {
  try {
    await saveProfile(true);
    edit(el("preset").value);
    say("Saved; " + state.presets[el("preset").value].label + " is the provider in use.");
  } catch (e) { say(e.message); }
});
el("save-key").addEventListener("click", async () => {
  const key = el("key").value;
  el("key").value = ""; // never kept on the page
  if (!key.trim()) { say("Paste a key first."); return; }
  const id = el("preset").value;
  try {
    if (!profile(id)) await saveProfile(!state.settings.profile);
    const r = await call("POST", "/cloud/profiles/" + encodeURIComponent(id) + "/key", { key });
    say("Key saved (ends with " + r.key_hint + ").");
    state = await call("GET", "/cloud/status");
    render();
    el("key-state").textContent = "Key saved (ends with " + r.key_hint + ").";
    await loadModels(false);
  } catch (e) { say("Not saved: " + e.message); }
});
el("delete-key").addEventListener("click", async () => {
  const id = el("preset").value;
  try {
    await call("DELETE", "/cloud/profiles/" + encodeURIComponent(id) + "/key");
    say("Key removed.");
    await load(id);
  } catch (e) { say(e.message); }
});
el("test").addEventListener("click", async () => {
  const id = el("preset").value;
  if (!profile(id)) { say("Save the provider first."); return; }
  say("Testing… (one tiny request to the provider)");
  try {
    const r = await call("POST", "/cloud/profiles/" + encodeURIComponent(id) + "/test");
    say(r.ok ? "Connected: " + r.model + " answered in " + r.seconds + " s." : "Not connected: " + r.detail);
  } catch (e) { say(e.message); }
});

load();
