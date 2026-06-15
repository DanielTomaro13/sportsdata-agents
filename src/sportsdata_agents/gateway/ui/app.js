/* sportsdata workbench UI — chat + history sidebar + agents/files/settings panes.
   Chat flow:  POST /message?mode=async → {task_id};  GET /tasks/{id}/events (SSE);
   GET /tasks/{id} → final answer.  History: GET /conversations, GET /conversations/{key}/messages. */

const API = "";                       // same-origin: the gateway serves this page
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const thread = $("#thread"), input = $("#input"), send = $("#send");
let convId = "web-" + Math.random().toString(36).slice(2, 10);
let busy = false;

const STARTERS = [
  "Compare tonight's AFL head-to-head odds across the books",
  "Scan for cross-book arbitrage right now",
  "Show the current Premier League table and top scorers",
  "What's the latest NBA Finals result and who leads the series?",
  "What can this platform actually do?",
];

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* tiny markdown → HTML (headings, bold, lists, tables, code, _sources_) */
function md(src) {
  const lines = String(src).split("\n");
  let html = "", i = 0;
  const inline = (t) => esc(t)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
  while (i < lines.length) {
    const ln = lines[i];
    if (/^\s*\|.*\|\s*$/.test(ln) && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1] || "")) {
      const row = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const head = row(ln); i += 2;
      let body = "";
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        const cells = row(lines[i]).map((c) =>
          /\*\*/.test(lines[i].split("|")[row(lines[i]).indexOf(c) + 1] || "")
            ? `<td class="best">${inline(c)}</td>` : `<td>${inline(c)}</td>`);
        body += `<tr>${cells.join("")}</tr>`; i++;
      }
      html += `<table><thead><tr>${head.map((h) => `<th>${inline(h)}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table>`;
      continue;
    }
    if (/^###?\s/.test(ln)) { html += `<h3>${inline(ln.replace(/^#+\s/, ""))}</h3>`; i++; continue; }
    if (/^\s*[-*]\s/.test(ln)) {
      let items = "";
      while (i < lines.length && /^\s*[-*]\s/.test(lines[i])) { items += `<li>${inline(lines[i].replace(/^\s*[-*]\s/, ""))}</li>`; i++; }
      html += `<ul>${items}</ul>`; continue;
    }
    if (/^\s*_.*_\s*$/.test(ln)) { html += `<p class="src">${inline(ln.replace(/^_|_$/g, ""))}</p>`; i++; continue; }
    if (ln.trim()) { html += `<p>${inline(ln)}</p>`; }
    i++;
  }
  return html;
}

function addMsg(role, who) {
  $("#welcome")?.remove();
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.innerHTML = `<div class="avatar">${who}</div><div class="body"></div>`;
  thread.appendChild(el);
  thread.scrollTop = thread.scrollHeight;
  return el.querySelector(".body");
}

async function health() {
  let ok = false;
  try {
    const r = await fetch(`${API}/healthz`);
    ok = r.ok && (await r.json()).ok;
  } catch { ok = false; }
  $("#dot").className = "sdot " + (ok ? "up" : "down");
  $("#dot").title = ok ? "connected" : "offline — start the app";
}

/* ─── chat ─────────────────────────────────────────────────── */
async function ask(text) {
  if (busy || !text.trim()) return;
  showPane("chat");
  busy = true; send.disabled = true;
  addMsg("user", "you").innerHTML = `<p>${esc(text)}</p>`;
  const body = addMsg("bot", "sd");
  const tools = document.createElement("div"); tools.className = "tools";
  const think = document.createElement("span"); think.className = "shimmer"; think.textContent = "thinking…";
  body.append(tools, think);
  thread.scrollTop = thread.scrollHeight;

  let taskId;
  try {
    const r = await fetch(`${API}/message?mode=async`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ text, conversation_id: convId }),
    });
    if (!r.ok) throw new Error(`gateway ${r.status}`);
    taskId = (await r.json()).task_id;
  } catch (e) {
    think.remove();
    body.innerHTML = `<p class="src">Couldn't reach the desk (${esc(e.message)}). Is the app running?</p>`;
    busy = false; send.disabled = false; return;
  }

  let done = false;
  const end = () => { if (done) return; done = true; es.close(); finish(taskId, body, think); };
  const es = new EventSource(`${API}/tasks/${taskId}/events`);
  es.onmessage = (ev) => {
    const d = JSON.parse(ev.data);
    if (d.event === "tool_call") {
      const row = document.createElement("div"); row.className = "tool";
      row.innerHTML = `<span class="${d.ok === false ? "fail" : "check"}">${d.ok === false ? "✕" : "✓"}</span><span class="nm">${esc(d.tool || "tool")}</span>`;
      tools.appendChild(row);
      thread.scrollTop = thread.scrollHeight;
    } else if (d.event === "end") {
      end();
    }
  };
  es.onerror = () => { if (!done) setTimeout(end, 500); };
}

async function finish(taskId, body, think) {
  try {
    const r = await fetch(`${API}/tasks/${taskId}`);
    const t = await r.json();
    const res = t.result;
    think.remove();
    if (!res) { body.innerHTML += `<p class="src">No answer (state: ${esc(t.state)}).</p>`; }
    else {
      const ans = document.createElement("div"); ans.innerHTML = md(res.answer || "(no answer)");
      body.appendChild(ans);
      if (res.sources?.length) {
        const s = document.createElement("div"); s.className = "src";
        s.textContent = "sources: " + res.sources.join(", "); body.appendChild(s);
      }
      const m = document.createElement("div"); m.className = "meta";
      m.innerHTML = `${res.tool_calls} tool call${res.tool_calls === 1 ? "" : "s"} · $${(res.cost_usd || 0).toFixed(4)}`
        + (res.verified ? " · verified ✓" : "") + ` · <span class="adv">advisory only</span>`;
      body.appendChild(m);
    }
  } catch (e) {
    think.remove();
    body.innerHTML += `<p class="src">Lost the answer (${esc(e.message)}).</p>`;
  }
  thread.scrollTop = thread.scrollHeight;
  busy = false; send.disabled = false; input.focus();
  loadHistory();  // the new turn just (maybe) created/updated a conversation
}

/* ─── chat history sidebar ─────────────────────────────────── */
function newChat() {
  convId = "web-" + Math.random().toString(36).slice(2, 10);
  thread.innerHTML = `<div class="welcome" id="welcome">
      <h1>Your <span class="g">sports-data desk</span></h1>
      <p>Ask the agent team — cross-book odds, models, arbitrage, fantasy. Every answer is grounded in live tool results.</p>
      <div class="chips" id="chips"></div></div>`;
  mountChips();
  markActiveConversation();
  showPane("chat");
  input.focus();
}

function timeAgo(iso) {
  if (!iso) return "";
  const d = (Date.now() - new Date(iso).getTime()) / 1000;
  if (d < 60) return "just now";
  if (d < 3600) return Math.floor(d / 60) + "m";
  if (d < 86400) return Math.floor(d / 3600) + "h";
  return Math.floor(d / 86400) + "d";
}

async function loadHistory() {
  let convs = [];
  try {
    const r = await fetch(`${API}/conversations`, { cache: "no-store" });
    if (r.ok) convs = (await r.json()).conversations || [];
  } catch { /* sidebar just stays empty */ }
  const hist = $("#hist");
  if (!convs.length) { hist.innerHTML = `<div class="empty">No conversations yet</div>`; return; }
  hist.innerHTML = `<div class="lbl">RECENT</div>` + convs.map((c) =>
    `<button class="citem" data-key="${esc(c.key)}" title="${esc(c.title)} · ${timeAgo(c.last_at)}">${esc(c.title)}</button>`
  ).join("");
  markActiveConversation();
}

function markActiveConversation() {
  $$("#hist .citem").forEach((b) => b.classList.toggle("active", b.dataset.key === convId));
}

async function openConversation(key) {
  let msgs = [];
  try {
    const r = await fetch(`${API}/conversations/${encodeURIComponent(key)}/messages`, { cache: "no-store" });
    if (r.ok) msgs = (await r.json()).messages || [];
  } catch { return; }
  convId = key;
  thread.innerHTML = "";
  for (const m of msgs) {
    if (m.role === "user") addMsg("user", "you").innerHTML = `<p>${esc(m.content)}</p>`;
    else if (m.role === "assistant") addMsg("bot", "sd").innerHTML = md(m.content || "");
  }
  if (!msgs.length) thread.innerHTML = `<div class="pane-empty">This conversation is empty.</div>`;
  markActiveConversation();
  showPane("chat");
  thread.scrollTop = thread.scrollHeight;
}

/* ─── pane router ──────────────────────────────────────────── */
function showPane(name) {
  $$(".pane").forEach((p) => p.classList.toggle("show", p.id === `pane-${name}`));
  $$("#nav button[data-pane]").forEach((b) => b.classList.toggle("active", b.dataset.pane === name));
  if (name === "agents") loadAgents();
  if (name === "files") loadFiles();
  if (name === "settings") loadSettings();
}

/* ─── agents pane ──────────────────────────────────────────── */
let agentCache = null;
async function loadAgents() {
  const body = $("#agents-body");
  try {
    const r = await fetch(`${API}/agents`, { cache: "no-store" });
    agentCache = await r.json();
  } catch (e) { body.innerHTML = `<div class="muted">Couldn't load agents (${esc(e.message)}).</div>`; return; }
  const ids = Object.keys(agentCache).sort((a, b) => {
    const pa = agentCache[a].plane === "ops" ? 1 : 0, pb = agentCache[b].plane === "ops" ? 1 : 0;
    return pa - pb || agentCache[a].display_name.localeCompare(agentCache[b].display_name);
  });
  const product = ids.filter((id) => agentCache[id].plane !== "ops");
  const ops = ids.filter((id) => agentCache[id].plane === "ops");
  $("#agents-sub").textContent = `${product.length} product · ${ops.length} ops`;
  const card = (id) => {
    const a = agentCache[id];
    const caps = (a.capabilities || []).slice(0, 6).map((c) => `<span class="tag">${esc(c)}</span>`).join("");
    const more = (a.capabilities || []).length > 6 ? `<span class="tag">+${a.capabilities.length - 6}</span>` : "";
    return `<div class="card click" data-agent="${esc(id)}">
      <h3>${esc(a.display_name)} <span class="badge ${a.plane}">${a.plane}</span></h3>
      <div class="desc">${esc(a.description || "No description.")}</div>
      <div class="tags">${caps}${more}</div></div>`;
  };
  body.innerHTML =
    `<div class="section-lbl">Product agents — these answer you in chat</div><div class="grid">${product.map(card).join("")}</div>` +
    (ops.length ? `<div class="section-lbl">Ops agents — platform maintenance (operator only)</div><div class="grid">${ops.map(card).join("")}</div>` : "");
}

function showAgentDetail(id) {
  const a = agentCache?.[id]; if (!a) return;
  const body = $("#agents-body");
  const list = (label, arr) => arr && arr.length
    ? `<div class="section-lbl">${label}</div><div class="tags">${arr.map((x) => `<span class="tag">${esc(x)}</span>`).join("")}</div>` : "";
  body.innerHTML = `<button class="backlink" id="agents-back">← All agents</button>
    <h2 style="margin:14px 0 4px;font-size:20px">${esc(a.display_name)} <span class="badge ${a.plane}">${a.plane}</span></h2>
    <div class="muted">${esc(a.description || "No description.")}</div>
    <div class="kv"><span>Model tier</span><b><code>${esc(a.tier)}</code></b></div>
    <div class="kv"><span>Version</span><b><code>${esc(a.version)}</code></b></div>
    ${a.deprecated ? `<div class="kv"><span>Deprecated</span><b style="color:var(--warn)">${esc(a.deprecated)}</b></div>` : ""}
    ${list("Data capabilities (MCP)", a.capabilities)}
    ${list("Native tools", a.native_tools)}
    ${list("Can delegate to", a.delegates_to)}
    ${list("Skills", a.skills)}`;
  $("#agents-back").addEventListener("click", loadAgents);
}

/* ─── files pane ───────────────────────────────────────────── */
function fmtSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}
async function loadFiles() {
  const body = $("#files-body");
  let data;
  try {
    const r = await fetch(`${API}/files`, { cache: "no-store" });
    data = await r.json();
  } catch (e) { body.innerHTML = `<div class="muted">Couldn't load files (${esc(e.message)}).</div>`; return; }
  $("#files-sub").textContent = data.desk_dir || "";
  const files = data.files || [];
  if (!files.length) {
    body.innerHTML = `<div class="pane-empty">No files yet.<br>Charts, CSVs and reports your agents create land here —<br>ask the desk to "export" or "save" something.</div>`;
    return;
  }
  body.innerHTML = files.map((f) =>
    `<div class="filerow" data-file="${esc(f.name)}">
      <div class="fic">${esc((f.ext || "•").slice(0, 4).toUpperCase())}</div>
      <div class="fn">${esc(f.name)}</div>
      <div class="fmeta">${fmtSize(f.size)} · ${timeAgo(f.modified)}</div>
    </div>`).join("");
}

/* ─── settings pane ────────────────────────────────────────── */
async function loadSettings() {
  const body = $("#settings-body");
  let s;
  try {
    const r = await fetch(`${API}/settings`, { cache: "no-store" });
    s = await r.json();
  } catch (e) { body.innerHTML = `<div class="muted">Couldn't load settings (${esc(e.message)}).</div>`; return; }
  const acc = s.account || {};
  const kv = (k, v) => `<div class="kv"><span>${esc(k)}</span><b>${v}</b></div>`;
  body.innerHTML =
    `<div class="section-lbl">Model</div>` +
    kv("Provider", s.provider ? esc(s.provider) : `<span style="color:var(--warn)">not configured</span>`) +
    kv("API key", s.model_key_configured ? `<span style="color:var(--ok)">configured ✓</span>` : `<span style="color:var(--bad)">missing</span>`) +
    kv("Plan", `<code>${esc((acc.tier || "—").toUpperCase())}</code>`) +
    `<div class="section-lbl">Storage</div>` +
    kv("Data folder", `<code>${esc(s.data_dir || "—")}</code>`) +
    kv("Warehouse", `<code>${esc(s.warehouse || "—")}</code>`) +
    kv("Desk folder", `<code>${esc(s.desk_dir || "—")}</code>`) +
    `<div class="section-lbl">Data providers (MCP)</div>` +
    `<div id="mcp-list"><div class="muted">Probing the data plane…</div></div>`;
  loadMcpGroups();
}

async function loadMcpGroups() {
  const el = $("#mcp-list"); if (!el) return;
  let data;
  try {
    const r = await fetch(`${API}/mcp/groups`, { cache: "no-store" });
    data = await r.json();
  } catch { el.innerHTML = `<div class="muted">Data plane unreachable.</div>`; return; }
  const provs = data.providers || [];
  if (!provs.length) { el.innerHTML = `<div class="muted">No providers reported — the data plane may be starting. Reopen Settings in a moment.</div>`; return; }
  el.innerHTML = `<div class="muted" style="margin-bottom:10px">${provs.length} providers · ${provs.reduce((n, p) => n + p.tools, 0)} tools. Per-provider live/needs-key status and on/off toggles arrive in the next update.</div>` +
    provs.map((p) =>
      `<div class="prov"><div class="ph"><span class="pn">${esc(p.provider)}</span><span class="pc">${p.tools} tools · ${p.groups.length} group${p.groups.length === 1 ? "" : "s"}</span></div>
       <div class="pg">${p.groups.map((g) => `<span class="tag">${esc(g.group)}</span>`).join("")}</div></div>`
    ).join("");
}

/* ─── account modal ────────────────────────────────────────── */
let account = null;
async function loadAccount() {
  try {
    const r = await fetch(`${API}/account`);
    if (!r.ok) { $("#tier-label").textContent = "Account"; return; }
    account = await r.json();
    $("#tier-label").textContent = account.tier ? account.tier.toUpperCase() : "Account";
  } catch { $("#tier-label").textContent = "Account"; }
}
function renderAccount() {
  if (!account) return;
  $("#acc-note").textContent = account.note || "";
  const quota = account.mcp_quota < 0 ? "unlimited" : account.mcp_quota;
  const agents = account.agents === "all" ? "all" : (account.agents || []).length;
  const addons = (account.addons || []).join(", ") || "none";
  $("#acc-list").innerHTML = [
    ["Tier", account.tier.toUpperCase()],
    ["MCP provider groups", quota],
    ["Chat interface", account.chat_ui ? "yes" : "no"],
    ["Desktop app", account.full_app ? "yes" : "no"],
    ["Agents", agents],
    ["Add-ons", addons],
  ].map(([k, v]) => `<li><span>${k}</span><b>${esc(v)}</b></li>`).join("");
}
async function renderSkills() {
  let learned = [];
  try {
    const r = await fetch(`${API}/skills`);
    if (r.ok) learned = ((await r.json()).skills || []).filter((s) => s.source === "user");
  } catch { /* panel just stays hidden */ }
  $("#acc-skills").hidden = learned.length === 0;
  $("#acc-skill-list").innerHTML = learned.map((s) =>
    `<li><span class="nm">${esc(s.name)}</span><span class="d">${esc(s.description)}</span>`
    + (s.recalls ? `<span class="n">used ${s.recalls}×</span>` : "")
    + `<span class="rm" data-name="${esc(s.name)}" title="Remove this learned skill">✕</span></li>`
  ).join("");
}
function openAccount() { renderAccount(); renderSkills(); $("#acc-msg").textContent = ""; $("#account").hidden = false; }
function closeAccount() { $("#account").hidden = true; }

/* ─── operator console ─────────────────────────────────────── */
async function detectOperator() {
  let isOp = false;
  try {
    const r = await fetch(`${API}/operator/overview`, { cache: "no-store" });
    isOp = r.ok;
  } catch { isOp = false; }
  $("#operator").hidden = !isOp;
  if (!isOp) $("#opmodal").hidden = true;
}
function opIcon(status) { return { ok: "✓", warn: "●", missing: "✗", info: "·" }[status] || "·"; }

async function renderOperator() {
  const msg = $("#op-msg"); msg.textContent = "";
  let d;
  try {
    const r = await fetch(`${API}/operator/overview`);
    if (!r.ok) throw new Error(`gateway ${r.status}`);
    d = await r.json();
  } catch (e) { msg.className = "msg err"; msg.textContent = "Couldn't load the console: " + e.message; return; }
  $("#op-preflight").innerHTML = (d.preflight?.checks || []).map((c) =>
    `<li><span class="st ${c.status}">${opIcon(c.status)}</span><span class="lbl">${esc(c.label)}</span><span class="det">${esc(c.detail)}</span></li>`).join("");
  const costs = d.costs;
  let costsHtml = "<div class='okv'><span>spend unavailable (warehouse offline)</span></div>";
  if (costs) {
    costsHtml = `<div class="okv"><span>Total</span><b>$${costs.total_usd.toFixed(4)}</b></div>`
      + `<div class="okv"><span>Ops / Product</span><b>$${costs.ops_usd.toFixed(4)} / $${costs.product_usd.toFixed(4)}</b></div>`
      + Object.entries(costs.by_agent || {}).slice(0, 5).map(([a, v]) =>
        `<div class="okv"><span>${esc(a)}</span><b>$${v.cost.toFixed(4)} <span style="opacity:.6">(${v.runs})</span></b></div>`).join("");
    if (d.budget) {
      const cls = d.budget.breached ? "breach" : "";
      const spent = d.budget.spent_usd == null ? "?" : `$${d.budget.spent_usd.toFixed(2)}`;
      const pct = d.budget.pct == null ? "" : ` — ${d.budget.pct}%`;
      costsHtml += `<div class="okv ${cls}"><span>Budget (${esc(d.budget.period)})</span><b>${spent} / $${d.budget.cap_usd.toFixed(2)}${pct}${d.budget.breached ? " OVER" : ""}</b></div>`;
    }
  }
  $("#op-costs").innerHTML = costsHtml;
  const ops = d.ops || {};
  let opsHtml = "";
  if ((ops.escalations || []).length)
    opsHtml += ops.escalations.map((e) => `<div class="okv"><span style="color:#ff8a8a">⚠ ${esc(e.summary || "?")}</span></div>`).join("");
  if ((ops.disabled_feeds || []).length)
    opsHtml += `<div class="okv"><span>Disabled feeds</span><b>${esc(ops.disabled_feeds.join(", "))}</b></div>`;
  opsHtml += Object.entries(ops.jobs || {}).map(([name, j]) => {
    const fails = j.consecutive_failures ? ` <span style="color:#ff8a8a">${j.consecutive_failures} fails</span>` : "";
    return `<div class="okv"><span>${esc(name)}</span><b style="font-weight:400;opacity:.75">${esc(j.schedule || "")}${fails}</b></div>`;
  }).join("");
  $("#op-ops").innerHTML = opsHtml || "<div class='okv'><span>nothing to report</span></div>";
  const agentSel = $("#op-agent"); const cur = agentSel.value; const agents = ops.agents || [];
  agentSel.innerHTML = agents.length
    ? agents.map((a) => `<option${a === cur ? " selected" : ""}>${esc(a)}</option>`).join("")
    : `<option value="">no ops agents</option>`;
}
let opTimer = null;
function openOperator() { renderOperator(); $("#opmodal").hidden = false; if (opTimer) clearInterval(opTimer); opTimer = setInterval(renderOperator, 15000); }
function closeOperator() { $("#opmodal").hidden = true; if (opTimer) { clearInterval(opTimer); opTimer = null; } }

async function runHealthAction() {
  const out = $("#op-health-out"), msg = $("#op-msg"); msg.textContent = "";
  out.innerHTML = "<div class='okv'><span>running health check…</span></div>";
  try {
    const r = await fetch(`${API}/operator/actions/health`, { method: "POST" });
    const d = await r.json();
    if (!r.ok) { out.innerHTML = ""; msg.className = "msg err"; msg.textContent = d.detail || "Health check failed."; return; }
    const h = d.health;
    const row = (label, ok, extra) => `<div class="okv ${ok ? "" : "breach"}"><span>${esc(label)}</span><b>${ok ? "✓" : "✗"}${extra ? " " + esc(extra) : ""}</b></div>`;
    out.innerHTML = row("doctor", h.doctor.ok, "") +
      row("feeds", h.feeds.stale_feeds.length === 0, `${h.feeds.providers_active_6h} active${h.feeds.stale_feeds.length ? `, ${h.feeds.stale_feeds.length} stale` : ""}`) +
      row("site", h.site.ok, h.site.ok ? `${h.site.latency_ms}ms` : String(h.site.error || "down"));
  } catch (e) { out.innerHTML = ""; msg.className = "msg err"; msg.textContent = e.message; }
}
async function runOpsAction() {
  const agent = $("#op-agent").value, prompt = $("#op-prompt").value.trim(), msg = $("#op-msg");
  if (!agent) { msg.className = "msg err"; msg.textContent = "Pick an ops agent."; return; }
  if (!prompt) { msg.className = "msg err"; msg.textContent = "Enter an instruction for the agent."; return; }
  msg.className = "msg"; msg.textContent = `Starting ${agent}…`;
  try {
    const r = await fetch(`${API}/operator/actions/run-ops`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ agent, prompt }),
    });
    const d = await r.json();
    if (!r.ok) { msg.className = "msg err"; msg.textContent = d.detail || "Couldn't start the run."; return; }
    msg.className = "msg ok"; msg.textContent = `Started ${agent} — the result lands in Ops plane shortly.`;
    $("#op-prompt").value = "";
  } catch (e) { msg.className = "msg err"; msg.textContent = e.message; }
}
async function setOperatorBudget() {
  const cap = parseFloat($("#op-cap").value); const msg = $("#op-msg");
  if (!(cap >= 0)) { msg.className = "msg err"; msg.textContent = "Enter a cap in USD."; return; }
  try {
    const r = await fetch(`${API}/operator/budget`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ cap_usd: cap, period: $("#op-period").value }),
    });
    const d = await r.json();
    if (!r.ok) { msg.className = "msg err"; msg.textContent = d.detail || "Couldn't set the budget."; return; }
    msg.className = "msg ok"; msg.textContent = `Budget set — $${d.budget.cap_usd}/${d.budget.period}.`;
    renderOperator();
  } catch (e) { msg.className = "msg err"; msg.textContent = e.message; }
}
async function activateKey() {
  const key = $("#acc-key").value.trim(); const msg = $("#acc-msg");
  if (!key) { msg.className = "msg err"; msg.textContent = "Paste your licence key first."; return; }
  msg.className = "msg"; msg.textContent = "Activating…";
  try {
    const r = await fetch(`${API}/account/activate`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ key }),
    });
    const d = await r.json();
    if (!r.ok) { msg.className = "msg err"; msg.textContent = d.detail || "That key did not activate."; return; }
    account = d.account; renderAccount();
    $("#tier-label").textContent = account.tier.toUpperCase();
    $("#acc-key").value = "";
    msg.className = "msg ok"; msg.textContent = `Activated — you're on ${account.tier.toUpperCase()}.`;
  } catch (e) { msg.className = "msg err"; msg.textContent = "Couldn't reach the app: " + e.message; }
}

/* ─── wiring ───────────────────────────────────────────────── */
function mountChips() {
  const chips = $("#chips"); if (!chips) return;
  chips.innerHTML = STARTERS.map((s) => `<div class="chip">${esc(s)}</div>`).join("");
}
mountChips();
$("#pane-chat").addEventListener("click", (e) => { if (e.target.classList.contains("chip")) ask(e.target.textContent); });
send.addEventListener("click", () => { const t = input.value; input.value = ""; resize(); ask(t); });
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); const t = input.value; input.value = ""; resize(); ask(t); }
});
function resize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 160) + "px"; }
input.addEventListener("input", resize);

// sidebar: new chat + history + nav
$("#newchat").addEventListener("click", newChat);
$("#hist").addEventListener("click", (e) => { const b = e.target.closest(".citem"); if (b) openConversation(b.dataset.key); });
$("#nav").addEventListener("click", (e) => { const b = e.target.closest("button[data-pane]"); if (b) showPane(b.dataset.pane); });

// agents/files pane delegation
$("#agents-body").addEventListener("click", (e) => { const c = e.target.closest("[data-agent]"); if (c) showAgentDetail(c.dataset.agent); });
$("#files-body").addEventListener("click", (e) => { const f = e.target.closest("[data-file]"); if (f) window.open(`${API}/files/raw?name=${encodeURIComponent(f.dataset.file)}`, "_blank"); });

// account + operator modals
$("#tier").addEventListener("click", openAccount);
$("#acc-close").addEventListener("click", closeAccount);
$("#account").addEventListener("click", (e) => { if (e.target.id === "account") closeAccount(); });
$("#acc-activate").addEventListener("click", activateKey);
$("#acc-upgrade").addEventListener("click", () => { if (account?.upgrade_url) window.open(account.upgrade_url, "_blank"); });
$("#acc-skill-list").addEventListener("click", async (e) => {
  const name = e.target?.dataset?.name; if (!name) return;
  try { await fetch(`${API}/skills/remove`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ name }) }); }
  finally { renderSkills(); }
});
$("#operator").addEventListener("click", openOperator);
$("#op-close").addEventListener("click", closeOperator);
$("#opmodal").addEventListener("click", (e) => { if (e.target.id === "opmodal") closeOperator(); });
$("#op-setbudget").addEventListener("click", setOperatorBudget);
$("#op-health").addEventListener("click", runHealthAction);
$("#op-runops").addEventListener("click", runOpsAction);

health(); loadAccount(); detectOperator(); loadHistory();
setInterval(health, 15000); input.focus();
