// The cloud model page (owner decision 2026-11-26). Every value is shown with textContent,
// never HTML. Only clicks on this page (with the per-install token) change anything. Leaving
// Local only sends accept_warning only after the click on the warning's own button.
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

function mode() {
  const picked = document.querySelector("input[name=mode]:checked");
  return picked ? picked.value : "local";
}

function render() {
  const s = state.settings;
  document.querySelectorAll("input[name=mode]").forEach((r) => { r.checked = r.value === s.mode; });
  el("provider").value = s.provider;
  el("base-url").value = s.base_url;
  el("model").value = s.model;
  el("budget").value = s.monthly_budget_usd;
  el("price-in").value = s.price_input_per_mtok;
  el("price-out").value = s.price_output_per_mtok;
  el("key-state").textContent = state.key_saved
    ? "A key is saved for this provider."
    : (state.key_error || "No key saved for this provider.");
  const u = state.usage;
  el("usage").textContent = "This month: " + u.calls + " cloud calls, " +
    (u.prompt_tokens + u.completion_tokens).toLocaleString() + " tokens (" +
    u.prompt_tokens.toLocaleString() + " in, " + u.completion_tokens.toLocaleString() +
    " out), US$" + u.cost_usd.toFixed(4) + " of US$" + Number(s.monthly_budget_usd).toFixed(2) +
    "; " + u.fallbacks + " answered locally instead.";
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
  el("status").textContent = s.mode === "local"
    ? "Local only"
    : (s.mode === "cloud" ? "Cloud" : "Hybrid") + " · " + s.provider + " " + s.model;
  el("warning").hidden = true;
}

function overrides() {
  const out = {};
  document.querySelectorAll("#agents select").forEach((sel) => { out[sel.dataset.agent] = sel.value; });
  return out;
}

async function load() {
  try {
    state = await call("GET", "/cloud/status");
    render();
  } catch (e) {
    el("status").textContent = e.message;
  }
}

function needsWarning() {
  return mode() !== "local" &&
    (state.settings.mode === "local" || !state.settings.warning_accepted_at);
}

async function save() {
  if (needsWarning() && !accepted) {
    el("warning").hidden = false;
    el("warning").scrollIntoView({ behavior: "smooth" });
    el("result").textContent = "Read the warning and click to turn the cloud model on.";
    return;
  }
  try {
    state = await call("POST", "/cloud/settings", {
      mode: mode(),
      provider: el("provider").value,
      base_url: el("base-url").value,
      model: el("model").value.trim(),
      agent_overrides: overrides(),
      monthly_budget_usd: Number(el("budget").value || 0),
      price_input_per_mtok: Number(el("price-in").value || 0),
      price_output_per_mtok: Number(el("price-out").value || 0),
      accept_warning: accepted,
    });
    render();
    el("result").textContent = "Saved.";
  } catch (e) {
    el("result").textContent = e.message;
  } finally {
    accepted = false;
  }
}

el("save").addEventListener("click", save);
el("accept").addEventListener("click", () => { accepted = true; save(); });
el("cancel").addEventListener("click", () => {
  accepted = false;
  document.querySelector("input[name=mode][value=local]").checked = true;
  el("warning").hidden = true;
  el("result").textContent = "Nothing was changed.";
});
document.querySelectorAll("input[name=mode]").forEach((r) => r.addEventListener("change", () => {
  accepted = false;
  el("warning").hidden = !needsWarning();
}));
el("provider").addEventListener("change", () => {
  el("base-url").value = state.default_base_urls[el("provider").value] || "";
  el("key-state").textContent = "Save to see whether a key is stored for this provider.";
});
el("save-key").addEventListener("click", async () => {
  const key = el("key").value.trim();
  el("key").value = "";
  if (!key) return;
  try {
    await call("POST", "/cloud/key", { provider: el("provider").value, key });
    el("result").textContent = "Key saved in the Windows credential store.";
    await load();
  } catch (e) {
    el("result").textContent = e.message;
  }
});
el("delete-key").addEventListener("click", async () => {
  try {
    await call("DELETE", "/cloud/key/" + encodeURIComponent(el("provider").value));
    el("result").textContent = "Key removed.";
    await load();
  } catch (e) {
    el("result").textContent = e.message;
  }
});
el("test").addEventListener("click", async () => {
  el("result").textContent = "Testing… (one tiny request to the provider)";
  try {
    const r = await call("POST", "/cloud/test");
    el("result").textContent = r.ok
      ? "Connected: " + r.model + " answered in " + r.seconds + " s."
      : "Not connected: " + r.detail;
  } catch (e) {
    el("result").textContent = e.message;
  }
});

load();
