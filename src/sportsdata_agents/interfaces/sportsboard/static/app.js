// Sports board client — REST over the warehouse-backed API.
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const od = (v) => v == null ? "–" : (v < 10 ? v.toFixed(2) : v.toFixed(1));
  const money = (v) => v == null ? null : (v >= 1000 ? "$" + (v / 1000).toFixed(v >= 10000 ? 0 : 1) + "k" : "$" + Math.round(v));
  const SIDE = { home: "HOME", away: "AWAY", draw: "DRAW" };
  const SHARP = new Set(["Kalshi", "Polymarket", "Betfair", "Pinnacle"]);

  const state = { games: [], selected: null, detail: null, sportFilter: "ALL", search: "", expanded: {}, mode: "live", list: "games", specials: [] };
  const sgm = { legs: [], result: null, book: "fair", books: null };
  // list mode: "games" (two-sided, sharp-line) or "specials" (novelty/outrights)

  // live warehouse API, or a captured static REPLAY the page animates (the
  // GitHub Pages demo). Replay = an array of frames [{games, details}, …] the
  // board steps through over time, mirroring the racing board.
  const cfg = window.SB_CONFIG || {};
  const qs = new URLSearchParams(location.search);
  const apiBase = qs.get("api") || cfg.apiBase || "";
  let frames = null, replayFrame = 0, replayTimer = null, liveTimer = null;
  const isReplay = () => state.mode === "replay";
  async function ensureFrames() {
    if (frames) return frames;
    try {
      const j = await (await fetch(qs.get("replay") || cfg.replayUrl || "data/replay.json")).json();
      frames = Array.isArray(j) ? j : [j];  // back-compat: a single snapshot → one frame
    } catch { frames = []; }
    if (!frames.length) frames = [{ games: [], details: {} }];
    return frames;
  }
  const curFrame = () => frames[replayFrame % frames.length];
  function enterReplay() {
    if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
    state.mode = "replay";
    if (!replayTimer && cfg.animate !== false) replayTimer = setInterval(replayTick, 2600);
  }
  function replayTick() { replayFrame++; loadGames(); }
  async function api(path) {
    if (isReplay()) throw new Error("replay");
    return (await fetch(apiBase.replace(/^ws/, "http") + path)).json();
  }

  // Server timestamps are UTC but often naive — parse as UTC or the board
  // shows tonight's games LIVE all morning (a browser reads naive as local).
  const utc = (iso) => new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + "Z");
  function ttj(iso) {
    if (!iso) return { t: "", c: "" };
    const m = Math.round((utc(iso) - Date.now()) / 60000);
    if (m <= 0) return { t: "LIVE", c: "live" };
    if (m < 60) return { t: m + "m", c: m < 10 ? "soon" : "" };
    if (m < 2880) return { t: Math.floor(m / 60) + "h" + (m % 60) + "m", c: "" };
    return { t: Math.floor(m / 1440) + "d", c: "" };
  }
  const teamOf = (d, s) => s === "home" ? d.home : s === "away" ? d.away : "Draw";

  // ---------- games list ----------
  async function loadSpecials() {
    if (isReplay()) { state.specials = []; renderList(); return; }
    try {
      const j = await api("/api/specials");
      state.specials = j.specials || [];
    } catch { state.specials = state.specials || []; }
    renderList();
  }

  async function loadGames() {
    let d;
    if (isReplay()) { await ensureFrames(); d = { games: curFrame().games || [] }; }
    else {
      try { d = await api("/api/games?hours=17520"); }  // two years = everything scheduled
      catch { if (cfg.forceReplay || cfg.replayUrl) { enterReplay(); await ensureFrames(); d = { games: curFrame().games || [] }; } else { setConn(false); return; } }
    }
    setConn(true);
    state.games = d.games || [];
    $("s-games").textContent = state.games.length;
    renderSportFilters();
    renderList();  // respects the GAMES/SPECIALS toggle — a poll tick must not stomp specials
    if (state.selected && state.detail) refreshDetail();
    if (!state.selected && state.games.length) select(state.games[0].fixture_id);
  }

  function renderSportFilters() {
    const counts = {};
    for (const g of state.games) counts[g.sport] = (counts[g.sport] || 0) + 1;
    const sports = Object.keys(counts).sort((a, b) => counts[b] - counts[a] || a.localeCompare(b));
    $("gsports").innerHTML = ["ALL", ...sports].map((s) =>
      `<button class="schip ${state.sportFilter === s ? "on" : ""}" data-s="${esc(s)}">${s === "ALL" ? `ALL ${state.games.length}` : `${s.toUpperCase()} ${counts[s]}`}</button>`).join("");
    $("gsports").querySelectorAll(".schip").forEach((b) => b.onclick = () => { state.sportFilter = b.dataset.s; renderSportFilters(); renderGames(); });
  }

  function renderList() { state.list === "specials" ? renderSpecials() : renderGames(); }

  function renderSpecials() {
    const el = $("games");
    const q = state.search.trim().toLowerCase();
    const rows = (state.specials || [])
      .filter((x) => !q || (x.name + " " + x.category).toLowerCase().includes(q));
    $("games-count").textContent = rows.length || "";
    if (!rows.length) { el.innerHTML = `<div class="note">${q ? "no specials match" : "no novelty markets in the window yet — the ingest fills this live"}</div>`; return; }
    el.innerHTML = rows.map((x) => {
      const t = ttj(x.start_time);
      const sels = (x.selections || []).slice(0, 3)
        .map((s) => `${esc(s.selection)} $${s.best_odds.toFixed(2)}`).join(" · ");
      const more = x.n_selections > 3 ? ` · +${x.n_selections - 3} more` : "";
      const open = state.spOpen === x.fixture_id;
      const table = !open ? "" : `<div class="spdetail">` + (x.selections || []).map((s) =>
        `<div class="spline"><span class="spname">${esc(s.selection)}</span>` +
        Object.entries(s.prices || {}).map(([b, o]) => `<span class="spprice"><label>${esc(b)}</label>$${o.toFixed(2)}</span>`).join("") +
        `</div>`).join("") + `</div>`;
      return `<div class="sprow ${open ? "open" : ""}" data-sp="${esc(x.fixture_id)}">
        <div class="sphead"><div><span class="spcat">${esc((x.category || "").toUpperCase())}</span>
        <div class="gname">${esc(x.name)}</div>
        <div class="spsels">${sels}${more} · <span class="gsrc">${(x.sources || []).join(", ")}</span></div></div>
        <div class="ttj ${t.c}">${x.is_resolution_time ? `<span class="gsrc">resolves</span> ` : ""}${t.t}</div></div>
        ${table}
      </div>`;
    }).join("");
    el.querySelectorAll(".sprow").forEach((r) => r.onclick = () => {
      state.spOpen = state.spOpen === r.dataset.sp ? null : r.dataset.sp; renderList();
    });
  }

  function renderGames() {
    const el = $("games");
    const q = state.search.trim().toLowerCase();
    const rows = state.games
      .filter((g) => state.sportFilter === "ALL" || g.sport === state.sportFilter)
      .filter((g) => !q || (g.name + " " + g.sport).toLowerCase().includes(q));
    $("games-count").textContent = rows.length || "";
    if (!rows.length) { el.innerHTML = `<div class="note">${q || state.sportFilter !== "ALL" ? "no games match" : "no upcoming games priced yet — the ingest fills this live"}</div>`; return; }
    const bySport = new Map();
    for (const g of rows) { if (!bySport.has(g.sport)) bySport.set(g.sport, []); bySport.get(g.sport).push(g); }
    const order = [...bySport.keys()].sort((a, b) =>
      utc(bySport.get(a)[0].start_time) - utc(bySport.get(b)[0].start_time));
    const row = (g) => {
      const t = ttj(g.start_time);
      const favTeam = g.favourite === "home" ? g.home : g.favourite === "away" ? g.away : g.favourite;
      return `<div class="grow ${state.selected === g.fixture_id ? "sel" : ""}" data-id="${esc(g.fixture_id)}">
        <div><div class="gname">${esc(g.name)}</div>
        <div class="gsub"><span class="gsrc">${g.no_sharp ? `${g.book_count} books · no sharp line` : `${(g.sharp_sources || []).length} sharp · ${g.book_count} books`} · ${g.market_count} mkts</span>${g.favourite ? ` · <span class="fav">${esc(favTeam || "")} ${g.fav_prob ? (g.fav_prob * 100).toFixed(0) + "%" : ""}</span>` : ""}</div></div>
        <div class="ttj ${t.c}">${t.t}${g.bf_matched ? `<div class="gsrc">${money(g.bf_matched)}</div>` : ""}</div>
      </div>`;
    };
    el.innerHTML = order.map((sp) => {
      const gs = bySport.get(sp);
      return `<div class="ghead">${sp.toUpperCase().replace(/_/g, " ")} <span class="count">${gs.length}</span></div>` + gs.map(row).join("");
    }).join("");
    el.querySelectorAll(".grow").forEach((x) => x.onclick = () => select(x.dataset.id));
  }

  // ---------- detail ----------
  async function select(id) {
    state.selected = id; sgm.legs = []; sgm.result = null; state.expanded = {};
    renderList();
    $("detail").innerHTML = '<div class="empty"><div class="big">◪</div>loading…</div>';
    await refreshDetail(true);
    loadSgmBooks(id);  // deliberately not awaited — chips fill in when known
  }
  async function refreshDetail(fresh) {
    if (!state.selected) return;
    let d;
    if (isReplay()) { await ensureFrames(); d = (curFrame().details || {})[state.selected]; }
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
      // sharp rows iterate the fair; book-only rows (no sharp priced them)
      // iterate the union of sides the books quote — comparison IS the value
      const sels2 = Object.keys(m.fair).length ? Object.keys(m.fair)
        : [...new Set(Object.values(m.quotes || {}).flatMap((q2) => Object.keys(q2)))];
      const bestOf = (sel) => {
        let bb = null, bo = 0;
        for (const [bk, q2] of Object.entries(m.quotes || {}))
          if (q2[sel] > bo) { bo = q2[sel]; bb = bk; }
        return { bo, bb };
      };
      const rows = sels2.map((sel, i) => {
        const v = m.value[sel] || {};
        const hasFair = m.fair[sel] != null;
        const b = hasFair ? null : bestOf(sel);
        const inSgm = sgm.legs.some((l) => l.key === m.key + ":" + sel);
        return `<tr class="${i === 0 ? "mstart" : ""}">
          <td class="mk">${i === 0 ? `<span class="mexp" data-exp="${esc(m.key)}">${isExp ? "▾" : "▸"}</span>${esc(m.label)}${m.book_only ? ' <span class="bk">books</span>' : ""}` : ""}</td>
          <td class="sel">${esc(selLabel(m, sel))}</td>
          <td class="sharp">${hasFair ? od(v.fair_odds || (m.fair[sel] ? 1 / m.fair[sel] : null)) + `<span class="pp">${(m.fair[sel] * 100).toFixed(0)}%</span>` : "–"}</td>
          <td class="best">${hasFair ? (v.best_odds ? od(v.best_odds) : "–") + (v.best_book ? ` <span class="bk">${esc(v.best_book)}</span>` : "")
                                     : (b.bo ? od(b.bo) + ` <span class="bk">${esc(b.bb)}</span>` : "–")}</td>
          <td class="val ${v.value_pct > 0 ? "pos" : "neg"}">${v.value_pct != null ? (v.value_pct > 0 ? "+" : "") + v.value_pct + "%" : "·"}</td>
          <td class="addcell">${hasFair ? `<button class="addsgm ${inSgm ? "in" : ""}" data-mkey="${esc(m.key)}" data-sel="${esc(sel)}" title="add to same-game multi">${inSgm ? "✓" : "+ SGM"}</button>` : ""}</td>
        </tr>`;
      }).join("");
      return rows + (isExp ? bookGrid(m, d) : "");
    }).join("");

    const extras = (d.extra_markets || []).filter((m) => !q || m.label.toLowerCase().includes(q));
    const extraRows = extras.map((m) => {
      const isExp = state.expanded[m.key];
      const first = `<tr class="mstart"><td class="mk"><span class="mexp" data-exp="${esc(m.key)}">${isExp ? "▾" : "▸"}</span>${esc(m.label)}</td><td class="sel flatc" colspan="4">${Object.keys(m.selections).length} selections · ${m.n_books} book${m.n_books > 1 ? "s" : ""}</td><td></td></tr>`;
      if (!isExp) return first;
      return first + Object.entries(m.selections).map(([sel, prices]) =>
        `<tr><td></td><td class="sel">${esc(sel)}</td><td class="best" colspan="3">${Object.entries(prices).sort((a2, b2) => b2[1] - a2[1]).map(([bk, o]) => `${od(o)} <span class="bk">${esc(bk)}</span>`).join(" · ")}</td><td></td></tr>`).join("");
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
      <tbody>${marketRows || '<tr><td colspan="6" class="flatc" style="padding:14px">no markets match</td></tr>'}</tbody>
      ${extraRows ? `<tbody><tr><th colspan="6" style="text-align:left;padding:10px 8px 4px;font-size:10px;letter-spacing:.1em;color:var(--dim)">MORE MARKETS — book odds only (${extras.length})</th></tr>${extraRows}</tbody>` : ""}
      </table>
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
      else if (r.unavailable) res = `<span class="down">${esc(r.unavailable)}</span>${(r.unmatched || []).map((u) => `<div class="sgmnote">${esc(u)}</div>`).join("")}`;
      else if (r.book_odds) res = `<b class="up">$${r.book_odds.toFixed(2)}</b> ${esc(r.priced_by)} <span class="flatc">(${esc(r.fractional || "")})</span> · <span class="flatc">bookable quote</span>`;
      else res = `<b class="up">$${(r.fair_odds || 0).toFixed(2)}</b> ${r.priced_by === "engine" ? "engine" : "independent"} · <span class="flatc">${((r.fair_probability || 0) * 100).toFixed(2)}%</span>${r.correlation_lift && r.correlation_lift !== 1 ? ` · corr ×${r.correlation_lift.toFixed(2)}` : ""}`;
    }
    // pricer selector: fair (our model-free floor) + whichever books can quote
    const bk = sgm.book || "fair";
    const bchip = (id, label, avail, why) =>
      `<button class="sgmbook ${bk === id ? "on" : ""} ${avail ? "" : "off"}" data-bk="${id}"
        ${avail ? "" : `disabled title="${esc(why || "unavailable")}"`}>${label}</button>`;
    let chipsBk = bchip("fair", "FAIR", true);
    for (const [id, st] of Object.entries(sgm.books || {}))
      chipsBk += bchip(id, id.toUpperCase(), !!st.available, st.reason);
    return `<div class="sgm">
      <div class="sgmhead">SAME-GAME MULTI <span class="sub">book quotes are real, bookable prices</span><span class="sgmbooks">${chipsBk}</span><span class="sgmprev">${preview}</span></div>
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
      // market/selection/line ride along so the ENGINE can price the legs
      // jointly; without them it can't identify the leg and we'd silently
      // fall back to the independent product.
      sgm.legs.push({ key, label: lab, odds: o, prob: m.fair[sel],
                      market: m.family, selection: sel, line: m.line ?? null });
    }
    sgm.result = null; renderDetail();
  }

  function wire() {
    const root = $("detail");
    root.querySelectorAll(".addsgm").forEach((b) => b.onclick = () => addSgm(b.dataset.mkey, b.dataset.sel));
    root.querySelectorAll(".mexp").forEach((e) => e.onclick = () => { state.expanded[e.dataset.exp] = !state.expanded[e.dataset.exp]; renderDetail(); });
    root.querySelectorAll(".sgmchip").forEach((c) => c.onclick = () => { sgm.legs.splice(+c.dataset.rm, 1); sgm.result = null; renderDetail(); });
    const gen = $("sgmgen"); if (gen) gen.onclick = generate;
    root.querySelectorAll(".sgmbook").forEach((b) => b.onclick = () => { sgm.book = b.dataset.bk; sgm.result = null; renderDetail(); });
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

  // Market quotes in the shape the engine's correlation model expects, limited to
  // the families the chosen legs actually use (and the matching line):
  //   h2h → [home, away] · total → [line, over, under] · line → [line, home, away]
  //
  // These must be RAW book prices (overround > 1), not our de-vigged fair odds:
  // a fair pair sums to exactly 1.0, which the engine rightly rejects as a stale
  // or arbed quote. So take one coherent book's two-way price, sharps first.
  const SHARP_BOOKS = ["Pinnacle", "Betfair", "Kalshi", "Polymarket"];

  function twoWay(m, a, b) {
    const q = m.quotes || {};
    const books = Object.keys(q).sort((x, y) => {
      const ix = SHARP_BOOKS.indexOf(x), iy = SHARP_BOOKS.indexOf(y);
      return (ix < 0 ? 99 : ix) - (iy < 0 ? 99 : iy);
    });
    for (const bk of books) {
      const x = q[bk][a], y = q[bk][b];
      if (x > 1 && y > 1 && (1 / x + 1 / y) > 1) return [x, y];  // has vig → usable
    }
    return null;
  }

  function engineQuotes(d, legs) {
    const q = {};
    const want = new Map();
    for (const l of legs) if (l.market) want.set(l.market, l.line ?? null);
    for (const m of (d.markets || [])) {
      if (!want.has(m.family)) continue;
      const wl = want.get(m.family);
      if (wl != null && m.line != null && +m.line !== +wl) continue;  // wrong line
      const [a, b] = m.family === "total" ? ["over", "under"] : ["home", "away"];
      const pair = twoWay(m, a, b);
      if (!pair) continue;
      if (m.family === "h2h") q.h2h = pair;
      else if (m.line != null) q[m.family] = [+m.line, pair[0], pair[1]];
    }
    return q;
  }

  async function generate() {
    const d = state.detail;
    if (sgm.legs.length < 2) { sgm.result = { warning: "add at least 2 legs" }; return renderDetail(); }
    if (isReplay()) { sgm.result = independentSgm(sgm.legs); return renderDetail(); }
    const body = { sport: d.sport, fixture_id: d.fixture_id,
                   quotes: engineQuotes(d, sgm.legs), legs: sgm.legs };
    if (sgm.book && sgm.book !== "fair") body.bookmaker = sgm.book;
    try {
      sgm.result = await (await fetch(apiBase.replace(/^ws/, "http") + "/api/sgm", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      })).json();
    } catch { sgm.result = independentSgm(sgm.legs); }
    renderDetail();
  }

  // Which books can quote this fixture — refreshed when a detail panel opens.
  async function loadSgmBooks(fixtureId) {
    sgm.books = null;
    if (isReplay()) return;
    try {
      const r = await (await fetch(apiBase.replace(/^ws/, "http")
        + "/api/sgm/books?fixture_id=" + encodeURIComponent(fixtureId))).json();
      sgm.books = r.books || null;
    } catch { sgm.books = null; }
    renderDetail();
  }

  function setConn(ok) {
    const dot = $("conn"), l = $("conn-label");
    if (isReplay()) { dot.className = "dot rep"; l.textContent = "REPLAY"; const b = $("banner"); if (b) b.classList.add("show"); return; }
    dot.className = "dot" + (ok ? " on" : ""); l.textContent = ok ? "LIVE" : "OFFLINE";
  }

  $("gsearch").addEventListener("input", (e) => { state.search = e.target.value; renderList(); });
  $("mode").querySelectorAll(".mchip").forEach((b) => b.onclick = () => {
    state.list = b.dataset.m;
    $("mode").querySelectorAll(".mchip").forEach((c) => c.classList.toggle("on", c === b));
    $("list-title").textContent = state.list === "specials" ? "SPECIALS" : "GAMES";
    $("list-sub").textContent = state.list === "specials" ? "elections · futures · novelty" : "sharp line vs the books";
    $("gsports").style.display = state.list === "specials" ? "none" : "";
    if (state.list === "specials") loadSpecials(); else renderList();
  });
  const th = localStorage.getItem("sb-theme"); if (th) document.documentElement.setAttribute("data-theme", th);
  $("theme").onclick = () => {
    const c = document.documentElement.getAttribute("data-theme") === "light" ? "" : "light";
    if (c) document.documentElement.setAttribute("data-theme", c); else document.documentElement.removeAttribute("data-theme");
    localStorage.setItem("sb-theme", c);
  };
  setInterval(() => { $("clock").textContent = new Date().toLocaleTimeString("en-GB"); }, 1000);
  if (cfg.forceReplay && !apiBase) enterReplay();
  loadGames();
  if (!isReplay()) liveTimer = setInterval(loadGames, 15000);  // live re-polls; replay self-animates
})();
