/* sportsdata chat UI — drives the gateway's async + SSE flow.
   POST /message?mode=async  → {task_id}
   GET  /tasks/{id}/events    → SSE: run_start / tool_call / run_end / end
   GET  /tasks/{id}           → final {result:{answer, sources, ...}}        */

const API = "";                       // same-origin: the gateway serves this page
const convId = "web-" + Math.random().toString(36).slice(2, 10);
const $ = (s) => document.querySelector(s);
const thread = $("#thread"), input = $("#input"), send = $("#send");
let busy = false;

const STARTERS = [
  "Compare tonight's AFL head-to-head odds across the books",
  "Scan for cross-book arbitrage right now",
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
  try {
    const r = await fetch(`${API}/healthz`);
    const ok = r.ok && (await r.json()).ok;
    $("#dot").className = "dot " + (ok ? "up" : "down");
    $("#statustext").textContent = ok ? "connected" : "starting…";
  } catch {
    $("#dot").className = "dot down";
    $("#statustext").textContent = "offline — start the app";
  }
}

async function ask(text) {
  if (busy || !text.trim()) return;
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
      // the TASK end marker — run_end fires per sub-agent and is NOT terminal
      end();
    }
  };
  // a stream drop after work is in flight: fall back to polling the final result
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
}

/* account: show the tier in the header chip; click it to view/upgrade the plan */
let account = null;

async function loadAccount() {
  try {
    const r = await fetch(`${API}/account`);
    if (!r.ok) { $("#tier").textContent = ""; return; }
    account = await r.json();
    $("#tier").textContent = account.tier ? account.tier.toUpperCase() : "";
  } catch { $("#tier").textContent = ""; }
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
  // the skills the generalist has authored as it learned this user's needs
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

/* operator console — the panel only exists on the operator's own deployment
   (the gateway 404s /operator/* unless SPORTSDATA_OPERATOR is set) */
async function detectOperator() {
  try {
    const r = await fetch(`${API}/operator/overview`);
    if (r.ok) $("#operator").hidden = false;
  } catch { /* not the operator build — chip stays hidden */ }
}

function opIcon(status) {
  return { ok: "✓", warn: "●", missing: "✗", info: "·" }[status] || "·";
}

async function renderOperator() {
  const msg = $("#op-msg");
  msg.textContent = "";
  let d;
  try {
    const r = await fetch(`${API}/operator/overview`);
    if (!r.ok) throw new Error(`gateway ${r.status}`);
    d = await r.json();
  } catch (e) {
    msg.className = "msg err"; msg.textContent = "Couldn't load the console: " + e.message;
    return;
  }
  $("#op-preflight").innerHTML = (d.preflight?.checks || []).map((c) =>
    `<li><span class="st ${c.status}">${opIcon(c.status)}</span>` +
    `<span class="lbl">${esc(c.label)}</span><span class="det">${esc(c.detail)}</span></li>`).join("");

  const costs = d.costs;
  let costsHtml = "<div class='kv'><span>spend unavailable (warehouse offline)</span></div>";
  if (costs) {
    costsHtml = `<div class="kv"><span>Total</span><b>$${costs.total_usd.toFixed(4)}</b></div>`
      + `<div class="kv"><span>Ops / Product</span><b>$${costs.ops_usd.toFixed(4)} / $${costs.product_usd.toFixed(4)}</b></div>`
      + Object.entries(costs.by_agent || {}).slice(0, 5).map(([a, v]) =>
        `<div class="kv"><span>${esc(a)}</span><b>$${v.cost.toFixed(4)} <span style="opacity:.6">(${v.runs})</span></b></div>`).join("");
    if (d.budget) {
      const cls = d.budget.breached ? "breach" : "";
      const spent = d.budget.spent_usd == null ? "?" : `$${d.budget.spent_usd.toFixed(2)}`;
      const pct = d.budget.pct == null ? "" : ` — ${d.budget.pct}%`;
      costsHtml += `<div class="kv ${cls}"><span>Budget (${esc(d.budget.period)})</span>` +
        `<b>${spent} / $${d.budget.cap_usd.toFixed(2)}${pct}${d.budget.breached ? " OVER" : ""}</b></div>`;
    }
  }
  $("#op-costs").innerHTML = costsHtml;

  const ops = d.ops || {};
  let opsHtml = "";
  if ((ops.escalations || []).length) {
    opsHtml += ops.escalations.map((e) =>
      `<div class="kv"><span style="color:#ff8a8a">⚠ ${esc(e.summary || "?")}</span></div>`).join("");
  }
  if ((ops.disabled_feeds || []).length) {
    opsHtml += `<div class="kv"><span>Disabled feeds</span><b>${esc(ops.disabled_feeds.join(", "))}</b></div>`;
  }
  opsHtml += Object.entries(ops.jobs || {}).map(([name, j]) => {
    const fails = j.consecutive_failures ? ` <span style="color:#ff8a8a">${j.consecutive_failures} fails</span>` : "";
    return `<div class="kv"><span>${esc(name)}</span><b style="font-weight:400;opacity:.75">${esc(j.schedule || "")}${fails}</b></div>`;
  }).join("");
  $("#op-ops").innerHTML = opsHtml || "<div class='kv'><span>nothing to report</span></div>";

  const agentSel = $("#op-agent");
  const cur = agentSel.value;
  const agents = ops.agents || [];
  agentSel.innerHTML = agents.length
    ? agents.map((a) => `<option${a === cur ? " selected" : ""}>${esc(a)}</option>`).join("")
    : `<option value="">no ops agents</option>`;
}

let opTimer = null;
function openOperator() {
  renderOperator();
  $("#opmodal").hidden = false;
  if (opTimer) clearInterval(opTimer);
  opTimer = setInterval(renderOperator, 15000);  // live panel — auto-refresh
}
function closeOperator() {
  $("#opmodal").hidden = true;
  if (opTimer) { clearInterval(opTimer); opTimer = null; }
}

async function runHealthAction() {
  const out = $("#op-health-out"), msg = $("#op-msg");
  msg.textContent = "";
  out.innerHTML = "<div class='kv'><span>running health check…</span></div>";
  try {
    const r = await fetch(`${API}/operator/actions/health`, { method: "POST" });
    const d = await r.json();
    if (!r.ok) { out.innerHTML = ""; msg.className = "msg err"; msg.textContent = d.detail || "Health check failed."; return; }
    const h = d.health;
    const row = (label, ok, extra) =>
      `<div class="kv ${ok ? "" : "breach"}"><span>${esc(label)}</span>` +
      `<b>${ok ? "✓" : "✗"}${extra ? " " + esc(extra) : ""}</b></div>`;
    out.innerHTML =
      row("doctor", h.doctor.ok, "") +
      row("feeds", h.feeds.stale_feeds.length === 0,
        `${h.feeds.providers_active_6h} active${h.feeds.stale_feeds.length ? `, ${h.feeds.stale_feeds.length} stale` : ""}`) +
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
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ agent, prompt }),
    });
    const d = await r.json();
    if (!r.ok) { msg.className = "msg err"; msg.textContent = d.detail || "Couldn't start the run."; return; }
    msg.className = "msg ok"; msg.textContent = `Started ${agent} — the result lands in Ops plane shortly.`;
    $("#op-prompt").value = "";
  } catch (e) { msg.className = "msg err"; msg.textContent = e.message; }
}

async function setOperatorBudget() {
  const cap = parseFloat($("#op-cap").value);
  const msg = $("#op-msg");
  if (!(cap >= 0)) { msg.className = "msg err"; msg.textContent = "Enter a cap in USD."; return; }
  try {
    const r = await fetch(`${API}/operator/budget`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ cap_usd: cap, period: $("#op-period").value }),
    });
    const d = await r.json();
    if (!r.ok) { msg.className = "msg err"; msg.textContent = d.detail || "Couldn't set the budget."; return; }
    msg.className = "msg ok"; msg.textContent = `Budget set — $${d.budget.cap_usd}/${d.budget.period}.`;
    renderOperator();
  } catch (e) { msg.className = "msg err"; msg.textContent = e.message; }
}

async function activateKey() {
  const key = $("#acc-key").value.trim();
  const msg = $("#acc-msg");
  if (!key) { msg.className = "msg err"; msg.textContent = "Paste your licence key first."; return; }
  msg.className = "msg"; msg.textContent = "Activating…";
  try {
    const r = await fetch(`${API}/account/activate`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ key }),
    });
    const d = await r.json();
    if (!r.ok) { msg.className = "msg err"; msg.textContent = d.detail || "That key did not activate."; return; }
    account = d.account; renderAccount();
    $("#tier").textContent = account.tier.toUpperCase();
    $("#acc-key").value = "";
    msg.className = "msg ok"; msg.textContent = `Activated — you're on ${account.tier.toUpperCase()}.`;
  } catch (e) {
    msg.className = "msg err"; msg.textContent = "Couldn't reach the app: " + e.message;
  }
}

// wire up
$("#chips").innerHTML = STARTERS.map((s) => `<div class="chip">${esc(s)}</div>`).join("");
$("#chips").addEventListener("click", (e) => { if (e.target.classList.contains("chip")) ask(e.target.textContent); });
send.addEventListener("click", () => { const t = input.value; input.value = ""; resize(); ask(t); });
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); const t = input.value; input.value = ""; resize(); ask(t); }
});
function resize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 160) + "px"; }
input.addEventListener("input", resize);

// account modal wiring
$("#tier").addEventListener("click", openAccount);
$("#acc-close").addEventListener("click", closeAccount);
$("#account").addEventListener("click", (e) => { if (e.target.id === "account") closeAccount(); });
$("#acc-activate").addEventListener("click", activateKey);
$("#acc-upgrade").addEventListener("click", () => { if (account?.upgrade_url) window.open(account.upgrade_url, "_blank"); });
$("#acc-skill-list").addEventListener("click", async (e) => {
  const name = e.target?.dataset?.name;
  if (!name) return;
  try {
    await fetch(`${API}/skills/remove`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ name }),
    });
  } finally { renderSkills(); }
});

// operator console wiring (the chip only appears on the operator's deployment)
$("#operator").addEventListener("click", openOperator);
$("#op-close").addEventListener("click", closeOperator);
$("#opmodal").addEventListener("click", (e) => { if (e.target.id === "opmodal") closeOperator(); });
$("#op-setbudget").addEventListener("click", setOperatorBudget);
$("#op-health").addEventListener("click", runHealthAction);
$("#op-runops").addEventListener("click", runOpsAction);

health(); loadAccount(); detectOperator(); setInterval(health, 15000); input.focus();
