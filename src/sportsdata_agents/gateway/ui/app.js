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

async function tier() {
  // the gateway exposes the tier via /healthz isn't guaranteed; show a friendly default
  try {
    const r = await fetch(`${API}/agents`);
    if (r.ok) { const d = await r.json(); $("#tier").textContent = `${(d.agents || d).length || ""} agents`.trim() || "Pro"; }
  } catch { $("#tier").textContent = ""; }
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

health(); tier(); setInterval(health, 15000); input.focus();
