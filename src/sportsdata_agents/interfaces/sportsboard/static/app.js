// Sports board client — REST over the warehouse-backed API.
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const od = (v) => v == null ? "–" : (v < 10 ? v.toFixed(2) : v.toFixed(1));
  const money = (v) => v == null ? null : (v >= 1000 ? "$" + (v / 1000).toFixed(v >= 10000 ? 0 : 1) + "k" : "$" + Math.round(v));
  const SIDE = { home: "HOME", away: "AWAY", draw: "DRAW" };
  const SHARP = new Set(["Kalshi", "Polymarket", "Betfair", "Pinnacle"]);

  const state = { games: [], selected: null, detail: null, sportFilter: "ALL", search: "", expanded: {}, mode: "live" };
  const sgm = { legs: [], result: null };

  // live warehouse API, or a captured static snapshot (GitHub Pages demo).
  const cfg = window.SB_CONFIG || {};
  const apiBase = new URLSearchParams(location.search).get("api") || cfg.apiBase || "";
  let replay = null;  // {games:[...], details:{fixture_id: detail}}
  const isReplay = () => state.mode === "replay";
  async function ensureReplay() {
    if (replay) return replay;
    try { replay = await (await fetch(cfg.replayUrl || "data/replay.json")).json(); }
    catch { replay = { games: [], details: {} }; }
    return replay;
  }
  async function api(path) {
    if (isReplay()) throw new Error("replay");
    return (await fetch(apiBase.replace(/^ws/, "http") + path)).json();
  }

  function ttj(iso) {
    if (!iso) return { t: "", c: "" };
    const m = Math.round((new Date(iso) - Date.now()) / 60000);
    if (m <= 0) return { t: "LIVE", c: "live" };
    if (m < 60) return { t: m + "m", c: m < 10 ? "soon" : "" };
    if (m < 2880) return { t: Math.floor(m / 60) + "h" + (m % 60) + "m", c: "" };
    return { t: Math.floor(m / 1440) + "d", c: "" };
  }
  const teamOf = (d, s) => s === "home" ? d.home : s === "away" ? d.away : "Draw";

  // ---------- games list ----------
  async function loadGames() {
    let d;
    if (isReplay()) { d = await ensureReplay(); }
    else {
      try { d = await api("/api/games"); }
      catch { if (cfg.forceReplay || cfg.replayUrl) { state.mode = "replay"; d = await ensureReplay(); } else { setConn(false); return; } }
    }
    setConn(true);
    state.games = d.games || [];
    $("s-games").textContent = state.games.length;
    renderSportFilters();
    renderGames();
    if (state.selected && state.detail) refreshDetail();
    if (!state.selected && state.games.length) select(state.games[0].fixture_id);
  }

  function renderSportFilters() {
    const sports = [...new Set(state.games.map((g) => g.sport))].sort();
    $("gsports").innerHTML = ["ALL", ...sports].map((s) =>
      `<button class="schip ${state.sportFilter === s ? "on" : ""}" data-s="${esc(s)}">${s === "ALL" ? "ALL" : s.toUpperCase()}</button>`).join("");
    $("gsports").querySelectorAll(".schip").forEach((b) => b.onclick = () => { state.sportFilter = b.dataset.s; renderSportFilters(); renderGames(); });
  }

  function renderGames() {
    const el = $("games");
    const q = state.search.trim().toLowerCase();
    const rows = state.games
      .filter((g) => state.sportFilter === "ALL" || g.sport === state.sportFilter)
      .filter((g) => !q || (g.name + " " + g.sport).toLowerCase().includes(q));
    $("games-count").textContent = rows.length || "";
    if (!rows.length) { el.innerHTML = `<div class="note">${q || state.sportFilter !== "ALL" ? "no games match" : "no upcoming games priced yet — the ingest fills this live"}</div>`; return; }
    el.innerHTML = rows.map((g) => {
      const t = ttj(g.start_time);
      const favTeam = g.favourite === "home" ? g.home : g.favourite === "away" ? g.away : g.favourite;
      return `<div class="grow ${state.selected === g.fixture_id ? "sel" : ""}" data-id="${esc(g.fixture_id)}">
        <div><div class="gname">${esc(g.name)}</div>
        <div class="gsub">${g.sport.toUpperCase()} · <span class="gsrc">${(g.sharp_sources || []).length} sharp · ${g.market_count} mkts · ${g.book_count} books</span>${g.favourite ? ` · <span class="fav">${esc(favTeam || "")} ${g.fav_prob ? (g.fav_prob * 100).toFixed(0) + "%" : ""}</span>` : ""}</div></div>
        <div class="ttj ${t.c}">${t.t}${g.bf_matched ? `<div class="gsrc">${money(g.bf_matched)}</div>` : ""}</div>
      </div>`;
    }).join("");
    el.querySelectorAll(".grow").forEach((x) => x.onclick = () => select(x.dataset.id));
  }

  // ---------- detail ----------
  async function select(id) {
    state.selected = id; sgm.legs = []; sgm.result = null; state.expanded = {};
    renderGames();
    $("detail").innerHTML = '<div class="empty"><div class="big">◪</div>loading…</div>';
    await refreshDetail(true);
  }
  async function refreshDetail(fresh) {
    if (!state.selected) return;
    let d;
    if (isReplay()) { d = (await ensureReplay()).details[state.selected]; }
    else {
      try { d = await api("/api/game/" + encodeURIComponent(state.selected)); } catch { return; }
    }
    if (!d || d.error) { if (fresh) $("detail").innerHTML = '<div class="empty"><div class="big">◪</div>NO DATA</div>'; return; }
    state.detail = d;
    renderDetail();
  }

  function moneyFlowPanel(d) {
    const flow = d.flow || {};
    const moves = flow.moves || {};
    const series = flow.sharp_series || [];
    const sides = ["home", "away"].filter((s) => s in moves || (d.fair && s in d.fair));
    // who is the money moving to? biggest positive prob delta
    let toSide = null, best = 0;
    for (const s of sides) { const dv = (moves[s] || {}).delta || 0; if (dv > best) { best = dv; toSide = s; } }
    const spark = (side, col) => {
      const pts = series.map((p) => p[side]).filter((v) => v != null);
      if (pts.length < 2) return "";
      const mn = Math.min(...pts), mx = Math.max(...pts), sp = (mx - mn) || 1, step = 150 / (pts.length - 1);
      let path = "";
      pts.forEach((v, i) => { path += (i ? "L" : "M") + (i * step).toFixed(1) + "," + (26 - ((v - mn) / sp) * 22).toFixed(1); });
      return `<svg class="flowspark" viewBox="0 0 150 28"><path d="${path}" fill="none" stroke="${col}" stroke-width="1.6"/></svg>`;
    };
    const moveRow = (s) => {
      const m = moves[s]; if (!m) return "";
      const firm = m.delta > 0.004, drift = m.delta < -0.004;
      return `<div class="mvrow"><span class="mvteam">${esc(teamOf(d, s))}</span>
        <span class="mvspark">${spark(s, firm ? "var(--up)" : drift ? "var(--down)" : "var(--muted)")}</span>
        <span class="mv ${firm ? "up" : drift ? "down" : "flatc"}">${firm ? "▲" : drift ? "▼" : "•"} ${(m.open * 100).toFixed(0)}%→${(m.now * 100).toFixed(0)}%</span></div>`;
    };
    const matched = flow.matched_now, mIn = flow.matched_delta_60m;
    return `<div class="flowpanel">
      <div class="flowhead">MONEY FLOW <span class="sub">sharp line over ${flow.window_hours || 8}h${series.length ? "" : " — building…"}</span></div>
      ${sides.map(moveRow).join("") || '<div class="flatc" style="font-family:var(--mono);font-size:11px">no line history yet</div>'}
      <div class="flowfoot">
        ${toSide ? `<span class="tosig">💰 money to <b>${esc(teamOf(d, toSide))}</b></span>` : '<span class="flatc">line steady</span>'}
        ${matched != null ? `<span class="bfm">Betfair matched <b>${money(matched)}</b>${mIn ? ` · <span class="up">+${money(mIn)}/60m</span>` : ""}</span>` : ""}
      </div></div>`;
  }

  function bookGrid(m, d) {
    // full-industry ladder for one market: every source's price per selection
    const sels = Object.keys(m.fair);
    const srcs = Object.keys(m.quotes).sort((a, b) => (SHARP.has(b) - SHARP.has(a)));
    const selHead = (sel) => m.family === "h2h" ? esc(teamOf(d, sel) || SIDE[sel] || sel) : sel.toUpperCase();
    return `<tr class="expand"><td colspan="6"><table class="ladder">
      <thead><tr><th>SOURCE</th>${sels.map((s) => `<th>${selHead(s)}</th>`).join("")}</tr></thead>
      <tbody>${srcs.map((src) => {
        const isSharp = SHARP.has(src);
        return `<tr class="${isSharp ? "sh" : ""}"><td>${esc(src)}${isSharp ? ' <span class="stag">SHARP</span>' : ""}</td>${sels.map((sel) => {
          const o = (m.quotes[src] || {})[sel];
          const best = !isSharp && m.value[sel] && m.value[sel].best_book === src;
          return `<td class="${best ? "best" : ""}">${od(o)}</td>`;
        }).join("")}</tr>`;
      }).join("")}</tbody></table></td></tr>`;
  }

  function renderDetail() {
    const d = state.detail; if (!d) return;
    const t = ttj(d.start_time);
    const fair = d.fair || {};
    const sides = ["home", "away", "draw"].filter((s) => s in fair);
    const sharps = d.sharp_sources || [];
    const markets = d.markets || [];
    const q = (state._mq || "").toLowerCase();
    const shown = markets.filter((m) => !q || m.label.toLowerCase().includes(q));
    const cards = sides.map((s) => `<div class="sharpcard"><div class="side">${esc(teamOf(d, s) || SIDE[s])}</div>
      <div class="fairodds">${od(d.value[s] ? d.value[s].fair_odds : (fair[s] ? 1 / fair[s] : null))}</div>
      <div class="fairp">${(fair[s] * 100).toFixed(1)}% sharp</div></div>`).join("");

    const selLabel = (m, sel) => m.family === "h2h" ? (teamOf(d, sel) || SIDE[sel] || sel) : sel.toUpperCase();
    const marketRows = shown.map((m, mi) => {
      const isExp = state.expanded[m.key];
      const rows = Object.keys(m.fair).map((sel, i) => {
        const v = m.value[sel] || {};
        const inSgm = sgm.legs.some((l) => l.key === m.key + ":" + sel);
        return `<tr class="${i === 0 ? "mstart" : ""}">
          <td class="mk">${i === 0 ? `<span class="mexp" data-exp="${esc(m.key)}">${isExp ? "▾" : "▸"}</span>${esc(m.label)}` : ""}</td>
          <td class="sel">${esc(selLabel(m, sel))}</td>
          <td class="sharp">${od(v.fair_odds || (m.fair[sel] ? 1 / m.fair[sel] : null))}<span class="pp">${(m.fair[sel] * 100).toFixed(0)}%</span></td>
          <td class="best">${v.best_odds ? od(v.best_odds) : "–"}${v.best_book ? ` <span class="bk">${esc(v.best_book)}</span>` : ""}</td>
          <td class="val ${v.value_pct > 0 ? "pos" : "neg"}">${v.value_pct != null ? (v.value_pct > 0 ? "+" : "") + v.value_pct + "%" : "·"}</td>
          <td class="addcell"><button class="addsgm ${inSgm ? "in" : ""}" data-mkey="${esc(m.key)}" data-sel="${esc(sel)}" title="add to same-game multi">${inSgm ? "✓" : "+ SGM"}</button></td>
        </tr>`;
      }).join("");
      return rows + (isExp ? bookGrid(m, d) : "");
    }).join("");

    const rating = d.engine_rating;
    $("detail").innerHTML = `
      <div class="dhead"><span class="sport">${d.sport.toUpperCase()}</span><h2>${esc(d.name)}</h2><span class="ttj">${t.t}</span></div>
      ${moneyFlowPanel(d)}
      <div class="sharpbar">
        ${cards}
        <div><div class="rating" style="margin-bottom:4px">SHARP FROM · ${markets.length} markets</div><div class="srcchips">${sharps.map((s) => `<span class="srcchip">${esc(s)}</span>`).join("") || '<span class="flatc">no sharp priced</span>'}</div></div>
        ${rating ? `<div class="rating">ENGINE RATING<br><b>${rating.home != null ? (rating.home * 100).toFixed(0) + "% " + esc(d.home) : ""}${rating.away != null ? " · " + (rating.away * 100).toFixed(0) + "% " + esc(d.away) : ""}</b></div>` : ""}
      </div>
      <div class="mktbar"><input type="search" id="mktsearch" placeholder="filter markets…" value="${esc(state._mq || "")}" autocomplete="off" /><span class="flatc" style="font-family:var(--mono);font-size:10px">click ▸ for every book · + SGM to build a multi</span></div>
      <table class="mkts"><thead><tr><th>MARKET</th><th>SELECTION</th><th>SHARP</th><th>BEST BOOK</th><th>VALUE</th><th></th></tr></thead>
      <tbody>${marketRows || '<tr><td colspan="6" class="flatc" style="padding:14px">no markets match</td></tr>'}</tbody></table>
      ${sgmPanel()}
      <div class="legend">sharp = de-vigged blend of ${sharps.join(" · ") || "—"} over every market · <span class="up">green</span> = best book vs sharp · money flow = sharp line movement + Betfair matched over time</div>`;
    wire();
  }

  function sgmPanel() {
    const chips = sgm.legs.map((l, i) => `<span class="sgmchip" data-rm="${i}">${esc(l.label)} @${l.odds.toFixed(2)} ✕</span>`).join("");
    const r = sgm.result;
    // live independent preview from the legs' probs
    let preview = "";
    if (sgm.legs.length >= 2) {
      const p = sgm.legs.reduce((a, l) => a * l.prob, 1);
      preview = `<span class="flatc">indep ~$${(1 / p).toFixed(2)}</span>`;
    }
    let res = `<span class="flatc">click + SGM on any market, then generate</span>`;
    if (r) {
      if (r.warning) res = `<span class="down">${esc(r.warning)}</span>`;
      else res = `<b class="up">$${(r.fair_odds || 0).toFixed(2)}</b> ${r.priced_by === "engine" ? "engine" : "independent"} · <span class="flatc">${((r.fair_probability || 0) * 100).toFixed(2)}%</span>${r.correlation_lift && r.correlation_lift !== 1 ? ` · corr ×${r.correlation_lift.toFixed(2)}` : ""}`;
    }
    return `<div class="sgm">
      <div class="sgmhead">SAME-GAME MULTI <span class="sub">engine correlated price, else independent</span><span class="sgmprev">${preview}</span></div>
      <div class="sgmchips">${chips || '<span class="flatc">no legs — click <b>+ SGM</b> on the markets above</span>'}</div>
      <div class="sgmrow">
        <button class="sgmgen" id="sgmgen">⚡ Generate price</button>
        <button class="sgmbtn" id="sgmclear">clear</button>
        <span class="sgmresult">${res}</span>
      </div>
      ${r && r.warnings && r.warnings.length ? `<div class="sgmnote">${esc(r.warnings[0])}</div>` : ""}
    </div>`;
  }

  function addSgm(mkey, sel) {
    const d = state.detail;
    const m = (d.markets || []).find((x) => x.key === mkey); if (!m) return;
    const key = mkey + ":" + sel;
    const i = sgm.legs.findIndex((l) => l.key === key);
    if (i >= 0) { sgm.legs.splice(i, 1); }  // toggle off
    else {
      const v = m.value[sel] || {};
      const o = v.fair_odds || (m.fair[sel] ? 1 / m.fair[sel] : null);
      const lab = m.family === "h2h" ? teamOf(d, sel)
        : `${m.label.replace("Head to Head", "H2H").replace("Total O/U", "O/U")} ${sel}`;
      sgm.legs.push({ key, label: lab, odds: o, prob: m.fair[sel] });
    }
    sgm.result = null; renderDetail();
  }

  function wire() {
    const root = $("detail");
    root.querySelectorAll(".addsgm").forEach((b) => b.onclick = () => addSgm(b.dataset.mkey, b.dataset.sel));
    root.querySelectorAll(".mexp").forEach((e) => e.onclick = () => { state.expanded[e.dataset.exp] = !state.expanded[e.dataset.exp]; renderDetail(); });
    root.querySelectorAll(".sgmchip").forEach((c) => c.onclick = () => { sgm.legs.splice(+c.dataset.rm, 1); sgm.result = null; renderDetail(); });
    const gen = $("sgmgen"); if (gen) gen.onclick = generate;
    const clr = $("sgmclear"); if (clr) clr.onclick = () => { sgm.legs = []; sgm.result = null; renderDetail(); };
    const ms = $("mktsearch"); if (ms) ms.oninput = (e) => { state._mq = e.target.value; const p = ms.selectionStart; renderDetail(); const n = $("mktsearch"); if (n) { n.focus(); n.setSelectionRange(p, p); } };
  }

  function independentSgm(legs) {
    // same shape /api/sgm returns with no engine — the honest client-side floor
    const p = legs.reduce((a, l) => a * l.prob, 1);
    return { fair_probability: p, fair_odds: +(1 / p).toFixed(2), correlation_lift: 1,
             priced_by: "independent",
             warnings: ["no engine connected — legs priced independently; a real "
               + "same-game multi is usually SHORTER than this"] };
  }

  async function generate() {
    const d = state.detail;
    if (sgm.legs.length < 2) { sgm.result = { warning: "add at least 2 legs" }; return renderDetail(); }
    if (isReplay()) { sgm.result = independentSgm(sgm.legs); return renderDetail(); }
    try {
      sgm.result = await (await fetch(apiBase.replace(/^ws/, "http") + "/api/sgm", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sport: d.sport, fixture_id: d.fixture_id, legs: sgm.legs }),
      })).json();
    } catch { sgm.result = independentSgm(sgm.legs); }
    renderDetail();
  }

  function setConn(ok) {
    const dot = $("conn"), l = $("conn-label");
    if (isReplay()) { dot.className = "dot rep"; l.textContent = "SNAPSHOT"; return; }
    dot.className = "dot" + (ok ? " on" : ""); l.textContent = ok ? "LIVE" : "OFFLINE";
  }

  $("gsearch").addEventListener("input", (e) => { state.search = e.target.value; renderGames(); });
  const th = localStorage.getItem("sb-theme"); if (th) document.documentElement.setAttribute("data-theme", th);
  $("theme").onclick = () => {
    const c = document.documentElement.getAttribute("data-theme") === "light" ? "" : "light";
    if (c) document.documentElement.setAttribute("data-theme", c); else document.documentElement.removeAttribute("data-theme");
    localStorage.setItem("sb-theme", c);
  };
  setInterval(() => { $("clock").textContent = new Date().toLocaleTimeString("en-GB"); }, 1000);
  if (cfg.forceReplay && !apiBase) state.mode = "replay";
  loadGames();
  if (!isReplay()) setInterval(loadGames, 15000);  // a static snapshot doesn't re-poll
})();
