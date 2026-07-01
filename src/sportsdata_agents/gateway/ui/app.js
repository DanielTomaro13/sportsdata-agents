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

// Inline expandable trace for a chat reply (B4): fetch the turn's run and show the model's
// thinking + tool results under the message. Reuses the Agents-pane trace styles.
async function chatTrace(runId, container, link) {
  if (container.dataset.loaded) { // already fetched — just toggle
    container.hidden = !container.hidden;
    link.textContent = container.hidden ? "▸ trace" : "▾ trace";
    return;
  }
  link.textContent = "… trace";
  let d;
  try {
    const r = await fetch(`${API}/runs/${encodeURIComponent(runId)}`, { cache: "no-store" });
    d = await r.json();
    if (!r.ok) throw new Error(d.detail || "not found");
  } catch (e) {
    container.innerHTML = `<div class="muted">Couldn't load the trace (${esc(e.message)}).</div>`;
    container.hidden = false; container.dataset.loaded = "1"; link.textContent = "▾ trace"; return;
  }
  const roleLabel = (r) => r === "assistant" ? "thinking / reply" : r === "tool" ? "tool result" : r === "user" ? "task" : r;
  const msg = (mm) =>
    `<div class="tmsg ${esc(mm.role)}"><div class="trole">${esc(roleLabel(mm.role))}</div>${esc(mm.content || "")}` +
    `${mm.tools && mm.tools.length ? `<div class="ttools">${mm.tools.map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</div>` : ""}</div>`;
  container.innerHTML = `<div class="trace">${(d.transcript || []).map(msg).join("") || `<div class="muted">No transcript recorded for this turn.</div>`}</div>`;
  container.hidden = false; container.dataset.loaded = "1"; link.textContent = "▾ trace";
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
      if (res.run_id) m.innerHTML += ` · <span class="tracelink" style="cursor:pointer;color:var(--accent,#1f6feb)">▸ trace</span>`;
      body.appendChild(m);
      if (res.run_id) {
        const tc = document.createElement("div"); tc.className = "ctrace"; tc.hidden = true;
        body.appendChild(tc);
        m.querySelector(".tracelink").addEventListener("click", (ev) => chatTrace(res.run_id, tc, ev.target));
      }
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

let showArchived = false;
async function loadHistory() {
  let convs = [];
  try {
    const r = await fetch(`${API}/conversations?include_archived=1`, { cache: "no-store" });
    if (r.ok) convs = (await r.json()).conversations || [];
  } catch { /* sidebar just stays empty */ }
  const active = convs.filter((c) => !c.archived);
  const archived = convs.filter((c) => c.archived);
  const hist = $("#hist");
  if (!convs.length) { hist.innerHTML = `<div class="empty">No conversations yet</div>`; return; }
  const row = (c) => `<div class="crow" data-key="${esc(c.key)}">
      <button class="citem" data-key="${esc(c.key)}" title="${esc(c.title)} · ${timeAgo(c.last_at)}">${esc(c.title)}</button>
      <button class="ckebab" data-key="${esc(c.key)}" data-archived="${c.archived ? 1 : 0}" title="More">⋯</button>
    </div>`;
  let html = active.length ? `<div class="lbl">RECENT</div>` + active.map(row).join("") : "";
  if (archived.length) {
    html += `<button class="arch-toggle" id="arch-toggle">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="transform:rotate(${showArchived ? 90 : 0}deg);transition:transform .15s"><path d="M9 6l6 6-6 6"/></svg>
        Archived (${archived.length})</button>`;
    if (showArchived) html += archived.map(row).join("");
  }
  hist.innerHTML = html || `<div class="empty">No conversations yet</div>`;
  markActiveConversation();
}

function markActiveConversation() {
  $$("#hist .crow").forEach((b) => b.classList.toggle("active", b.dataset.key === convId));
}

/* per-conversation actions: a shared ⋯ menu, inline rename, two-click delete */
function openChatMenu(kebab) {
  const menu = $("#cmenu");
  const key = kebab.dataset.key, archived = kebab.dataset.archived === "1";
  menu.dataset.key = key; menu.dataset.archived = archived ? "1" : "0";
  $("#cmenu-archive-label").textContent = archived ? "Unarchive" : "Archive";
  $("#cmenu-delete-label").textContent = "Delete";  // reset any prior confirm state
  menu.querySelector('[data-act="delete"]').classList.remove("confirm");
  const r = kebab.getBoundingClientRect();
  menu.hidden = false;
  // place below-right of the kebab, clamped to the viewport
  menu.style.left = Math.min(r.left, window.innerWidth - menu.offsetWidth - 8) + "px";
  menu.style.top = Math.min(r.bottom + 4, window.innerHeight - menu.offsetHeight - 8) + "px";
  $$(".ckebab").forEach((k) => k.classList.toggle("open", k === kebab));
}
function closeChatMenu() {
  $("#cmenu").hidden = true;
  $$(".ckebab").forEach((k) => k.classList.remove("open"));
}

async function archiveConv(key, archived) {
  try {
    await fetch(`${API}/conversations/${encodeURIComponent(key)}/archive`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ archived }),
    });
  } finally { loadHistory(); }
}
async function deleteConv(key) {
  try {
    await fetch(`${API}/conversations/${encodeURIComponent(key)}`, { method: "DELETE" });
  } finally {
    if (key === convId) newChat(); else loadHistory();
  }
}
function startRename(key) {
  const row = $(`.crow[data-key="${CSS.escape(key)}"]`);
  if (!row) return;
  const btn = row.querySelector(".citem");
  const cur = btn.textContent;
  const input = document.createElement("input");
  input.className = "cedit"; input.value = cur;
  row.replaceChild(input, btn); input.focus(); input.select();
  const commit = async (save) => {
    const title = input.value.trim();
    if (save && title && title !== cur) {
      try {
        await fetch(`${API}/conversations/${encodeURIComponent(key)}/rename`, {
          method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ title }),
        });
      } catch { /* fall through to reload */ }
    }
    loadHistory();
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); commit(true); }
    else if (e.key === "Escape") { e.preventDefault(); commit(false); }
  });
  input.addEventListener("blur", () => commit(true));
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
let monTimer = null;
function showPane(name) {
  $$(".pane").forEach((p) => p.classList.toggle("show", p.id === `pane-${name}`));
  $$("#nav button[data-pane]").forEach((b) => b.classList.toggle("active", b.dataset.pane === name));
  if (monTimer) { clearInterval(monTimer); monTimer = null; }  // stop the monitors poll when leaving
  if (name === "agents") loadAgents();
  if (name === "files") loadFiles();
  if (name === "settings") loadSettings();
  if (name === "monitors") { loadMonitors(); monTimer = setInterval(loadMonitors, 20000); }
  if (name === "market") loadMarketplace();
}

/* ─── marketplace pane (storefront → browser checkout handoff) ── */
async function loadMarketplace() {
  const el = $("#market-body"); if (!el) return;
  let mk = null;
  try {
    const r = await fetch(`${API}/marketplace`, { cache: "no-store" });
    if (r.ok) mk = await r.json();
  } catch { /* fall through to the unreachable message */ }
  if (!mk) { el.innerHTML = `<div class="muted">Marketplace unreachable.</div>`; return; }
  const acc = mk.account || {};
  const addons = (acc.addons || []).length ? acc.addons.join(", ") : "none";
  const plan = (p) => `<div class="card">
      <h3>${esc(p.name)} <span class="pc" style="float:right">$${p.usd_month}/mo</span></h3>
      <div class="desc">${esc(p.desc)}</div>
      <div style="margin-top:10px"><button class="mkt-buy" data-url="${esc(p.url)}">Buy in browser ↗</button></div>
    </div>`;
  el.innerHTML =
    `<div class="section-lbl">Your plan</div>
     <div class="kv"><span>Tier</span><b><code>${esc(acc.tier || "free")}</code></b></div>
     <div class="kv"><span>Data feeds</span><b>${acc.mcp_quota === null ? "all" : esc(String(acc.mcp_quota ?? "—"))}</b></div>
     <div class="kv"><span>Add-ons</span><b>${esc(addons)}</b></div>
     <div class="section-lbl">Plans &amp; add-ons</div>
     <div class="grid">${(mk.plans || []).map(plan).join("")}</div>
     <div class="section-lbl">Your feeds</div>
     <div class="muted" style="margin-bottom:8px">Pick or swap which data feeds your licence unlocks — the picker opens in your browser with your licence key.</div>
     <button class="mkt-buy" id="mkt-feeds" data-url="${esc(mk.feeds_url)}">Choose / manage feeds ↗</button>
     <div class="muted" style="margin-top:18px;font-size:12px">Checkout happens on Stripe-hosted pages in your browser. After buying, your licence email arrives with the key — activate it under the account menu.</div>`;
  $$(".mkt-buy").forEach((b) =>
    b.addEventListener("click", () => { if (b.dataset.url) window.open(b.dataset.url, "_blank"); }));
}

/* ─── monitors pane (arb / line-move / value alerts) ─────────── */
async function loadMonitors() {
  const el = $("#mon-body"); if (!el) return;
  let alerts = [];
  try {
    const r = await fetch(`${API}/alerts?limit=80`, { cache: "no-store" });
    if (r.ok) alerts = (await r.json()).alerts || [];
  } catch { el.innerHTML = `<div class="muted">Data plane unreachable.</div>`; return; }
  if (!alerts.length) {
    el.innerHTML = `<div class="pane-empty">No alerts yet.<br>Set up an arbitrage, line-move or value watch (ask the desk, or <code>agents watch</code>) and fired alerts land here, newest first.</div>`;
    return;
  }
  const label = { arb: "arbitrage", line_move: "line move", value: "value" };
  const color = { arb: "var(--ok,#3fb950)", line_move: "var(--acc2,#d29922)", value: "var(--accent,#1f6feb)" };
  el.innerHTML = alerts.map((a) => {
    const k = a.kind || "alert";
    return `<div class="prov" style="border-left:3px solid ${color[k] || "var(--border)"}">
      <div class="ph">
        <span class="pstat" style="background:${color[k] || "#666"};color:#fff;padding:1px 8px;border-radius:10px;font-size:10px">${esc(label[k] || k)}</span>
        <span class="pc" style="margin-left:auto">${esc(timeAgo(a.created_at))}</span>
      </div>
      <div style="margin-top:6px;font-size:13px;white-space:pre-wrap">${esc(a.message || "")}</div>
    </div>`;
  }).join("");
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
    const pin = a.tier_override ? `<span class="tag" style="color:var(--accent)">model: ${esc(a.tier_override)}</span>` : "";
    return `<div class="card click" data-agent="${esc(id)}">
      <h3>${esc(a.display_name)} <span class="badge ${a.plane}">${a.plane}</span></h3>
      <div class="desc">${esc(a.description || "No description.")}</div>
      <div class="tags">${pin}${caps}${more}</div></div>`;
  };
  body.innerHTML =
    `<div class="section-lbl">Product agents — these answer you in chat</div><div class="grid">${product.map(card).join("")}</div>` +
    (ops.length ? `<div class="section-lbl">Ops agents — platform maintenance (operator only)</div><div class="grid">${ops.map(card).join("")}</div>` : "");
}

let currentAgentId = null;
function showAgentDetail(id) {
  const a = agentCache?.[id]; if (!a) return;
  currentAgentId = id;
  const body = $("#agents-body");
  const list = (label, arr) => arr && arr.length
    ? `<div class="section-lbl">${label}</div><div class="tags">${arr.map((x) => `<span class="tag">${esc(x)}</span>`).join("")}</div>` : "";
  body.innerHTML = `<button class="backlink" id="agents-back">← All agents</button>
    <h2 style="margin:14px 0 4px;font-size:20px">${esc(a.display_name)} <span class="badge ${a.plane}">${a.plane}</span></h2>
    <div class="muted">${esc(a.description || "No description.")}</div>
    <div class="kv"><span>Model</span><b><select id="agent-model-sel" class="agent-model-sel">
      <option value=""${!a.tier_override ? " selected" : ""}>default (${esc(a.tier)})</option>
      ${["fast", "balanced", "strong"].map((t) => `<option value="${t}"${a.tier_override === t ? " selected" : ""}>${t}</option>`).join("")}
      ${a.tier_override && !["fast", "balanced", "strong"].includes(a.tier_override) ? `<option value="${esc(a.tier_override)}" selected>${esc(a.tier_override)}</option>` : ""}
    </select></b></div>
    <div class="kv"><span>Version</span><b><code>${esc(a.version)}</code></b></div>
    ${a.deprecated ? `<div class="kv"><span>Deprecated</span><b style="color:var(--warn)">${esc(a.deprecated)}</b></div>` : ""}
    ${list("Data capabilities (MCP)", a.capabilities)}
    ${list("Native tools", a.native_tools)}
    ${list("Can delegate to", a.delegates_to)}
    ${list("Skills", a.skills)}
    <div class="section-lbl">Recent activity</div>
    <div id="agent-runs"><div class="muted">Loading activity…</div></div>`;
  $("#agents-back").addEventListener("click", loadAgents);
  $("#agent-model-sel").addEventListener("change", async (e) => {
    const tier = e.target.value || null;
    try {
      const r = await fetch(`${API}/agents/model`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ agent: id, tier }),
      });
      if (r.ok) a.tier_override = tier; // keep the cache honest so cards show the pin
    } catch { /* the next /agents reload reflects the real state */ }
  });
  loadAgentRuns(id);
}

async function loadAgentRuns(id) {
  const el = $("#agent-runs"); if (!el) return;
  let runs = [];
  try {
    const r = await fetch(`${API}/agents/${encodeURIComponent(id)}/runs`, { cache: "no-store" });
    if (r.ok) runs = (await r.json()).runs || [];
  } catch { /* leave the loading text replaced below */ }
  if (!runs.length) {
    el.innerHTML = `<div class="muted">No runs recorded yet. Ask this agent something in chat, then come back to see what it did.</div>`;
    return;
  }
  el.innerHTML = runs.map((r) =>
    `<div class="runrow" data-run="${esc(r.id)}">
      <span class="rstat ${r.status === "ok" ? "ok" : r.status === "error" ? "error" : "running"}"></span>
      <span class="rtask">${esc(r.task || "(no task recorded)")}</span>
      <span class="rmeta">${r.is_delegation ? "delegated · " : ""}$${(r.cost_usd || 0).toFixed(4)} · ${timeAgo(r.created_at)}</span>
    </div>`).join("");
}

async function showRunTrace(runId, backAgentId) {
  const body = $("#agents-body");
  let d;
  try {
    const r = await fetch(`${API}/runs/${encodeURIComponent(runId)}`, { cache: "no-store" });
    d = await r.json();
    if (!r.ok) throw new Error(d.detail || "not found");
  } catch (e) {
    body.innerHTML = `<button class="backlink" id="trace-back">← Back</button><div class="muted" style="margin-top:12px">Couldn't load the trace (${esc(e.message)}).</div>`;
    $("#trace-back").addEventListener("click", () => showAgentDetail(backAgentId || currentAgentId));
    return;
  }
  const roleLabel = (r) => r === "assistant" ? "thinking / reply" : r === "tool" ? "tool result" : r === "user" ? "task" : r;
  const msg = (m) =>
    `<div class="tmsg ${esc(m.role)}"><div class="trole">${esc(roleLabel(m.role))}</div>${esc(m.content || "")}` +
    `${m.tools && m.tools.length ? `<div class="ttools">${m.tools.map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</div>` : ""}</div>`;
  const deleg = (c) =>
    `<div class="deleg" data-run="${esc(c.id)}" data-agent="${esc(c.agent)}"><span class="badge product">${esc(c.agent)}</span>` +
    `<span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(c.task || "")}</span><span class="rmeta">${esc(c.status)}</span></div>`;
  body.innerHTML = `<button class="backlink" id="trace-back">← ${esc(d.agent)} activity</button>
    <h2 style="margin:14px 0 4px;font-size:18px">${esc(d.task || "Run")}</h2>
    <div class="muted">${esc(d.agent)} · ${esc(d.status)} · $${(d.cost_usd || 0).toFixed(4)} · ${d.tokens_in || 0}/${d.tokens_out || 0} tok</div>
    ${d.error ? `<div class="kv"><span>Error</span><b style="color:var(--bad)">${esc(d.error)}</b></div>` : ""}
    ${(d.delegations || []).length ? `<div class="section-lbl">Delegated to other agents (click to follow)</div>${d.delegations.map(deleg).join("")}` : ""}
    <div class="section-lbl">Trace — what it did and said to itself</div>
    <div class="trace">${(d.transcript || []).map(msg).join("") || `<div class="muted">No transcript recorded for this run.</div>`}</div>`;
  $("#trace-back").addEventListener("click", () => showAgentDetail(backAgentId || currentAgentId));
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
  const needs = provs.filter((p) => p.status === "needs_key").length;
  const ready = provs.length - needs;
  const badge = (p) => p.status === "needs_key"
    ? `<span class="pstat needs">needs key</span>`
    : `<span class="pstat ready">ready</span>`;
  el.innerHTML =
    `<div class="muted" style="margin-bottom:12px">${ready} ready · ${needs} need a key. Toggle a provider <b>off</b> to stop it loading for every agent (takes effect on the next run). Open providers work without a key, though a few can be geo- or bot-blocked from your network.</div>` +
    provs.map((p) => {
      const on = p.enabled !== false; // default on when the flag is absent
      return `<div class="prov" style="${on ? "" : "opacity:.5"}">
        <div class="ph">
          <span class="pn">${esc(p.provider)}</span>${badge(p)}
          <span class="pc">${p.tools} tools · ${p.groups.length} group${p.groups.length === 1 ? "" : "s"}</span>
          <button class="ptog" data-prov="${esc(p.provider)}" data-on="${on ? 1 : 0}"
            title="${on ? "Disable" : "Enable"} ${esc(p.provider)}"
            style="margin-left:auto;cursor:pointer;border:1px solid var(--line,#30363d);border-radius:12px;padding:2px 12px;font-size:11px;background:${on ? "var(--accent,#1f6feb)" : "transparent"};color:${on ? "#fff" : "var(--muted,#8b949e)"}">${on ? "On" : "Off"}</button>
        </div>
        ${p.status === "needs_key" ? `<div class="pneed">add ${esc((p.auth_env || []).join(" + "))} to enable</div>` : ""}
        <div class="pg">${p.groups.map((g) => `<span class="tag">${esc(g.group)}</span>`).join("")}</div>
      </div>`;
    }).join("");
  el.querySelectorAll(".ptog").forEach((b) =>
    b.addEventListener("click", () => toggleProvider(b.dataset.prov, b.dataset.on !== "1")),
  );
}

async function toggleProvider(provider, enabled) {
  try {
    await fetch(`${API}/mcp/toggle`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ provider, enabled }),
    });
  } catch { /* a reload below reflects the real state regardless */ }
  loadMcpGroups();
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
$("#hist").addEventListener("click", (e) => {
  const kebab = e.target.closest(".ckebab");
  if (kebab) { e.stopPropagation(); openChatMenu(kebab); return; }
  if (e.target.closest("#arch-toggle")) { showArchived = !showArchived; loadHistory(); return; }
  const item = e.target.closest(".citem");
  if (item) openConversation(item.dataset.key);
});
$("#cmenu").addEventListener("click", (e) => {
  const btn = e.target.closest("button"); if (!btn) return;
  const menu = $("#cmenu"), key = menu.dataset.key, act = btn.dataset.act;
  if (act === "rename") { closeChatMenu(); startRename(key); }
  else if (act === "archive") { closeChatMenu(); archiveConv(key, menu.dataset.archived !== "1"); }
  else if (act === "delete") {
    if (!btn.classList.contains("confirm")) {  // two-click delete (no native confirm())
      btn.classList.add("confirm"); $("#cmenu-delete-label").textContent = "Click again to delete";
    } else { closeChatMenu(); deleteConv(key); }
  }
});
document.addEventListener("click", (e) => { if (!e.target.closest("#cmenu") && !e.target.closest(".ckebab")) closeChatMenu(); });
$("#nav").addEventListener("click", (e) => { const b = e.target.closest("button[data-pane]"); if (b) showPane(b.dataset.pane); });

// agents/files pane delegation
$("#agents-body").addEventListener("click", (e) => {
  const runEl = e.target.closest("[data-run]");  // run rows + delegation rows (check first)
  if (runEl) { showRunTrace(runEl.dataset.run, runEl.dataset.agent || currentAgentId); return; }
  const c = e.target.closest("[data-agent]");
  if (c) showAgentDetail(c.dataset.agent);
});
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
