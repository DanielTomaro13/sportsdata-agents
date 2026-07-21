// Sports board client — REST over the warehouse-backed API.
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const od = (v) => v == null ? "–" : (v < 10 ? v.toFixed(2) : v.toFixed(1));
  const money = (v) => v == null ? null : "$" + Math.round(v).toLocaleString();
  const SIDE = { home: "HOME", away: "AWAY", draw: "DRAW" };

  const state = { games: [], selected: null, detail: null };
  const sgm = { legs: [], result: null };

  function ttj(iso) {
    if (!iso) return { t: "", c: "" };
    const m = Math.round((new Date(iso) - Date.now()) / 60000);
    if (m <= 0) return { t: "LIVE", c: "live" };
    if (m < 60) return { t: m + "m", c: m < 10 ? "soon" : "" };
    if (m < 2880) return { t: Math.floor(m / 60) + "h" + (m % 60) + "m", c: "" };
    return { t: Math.floor(m / 1440) + "d", c: "" };
  }

  async function loadGames() {
    let d;
    try { d = await (await fetch("/api/games")).json(); } catch { setConn(false); return; }
    setConn(true);
    state.games = d.games || [];
    $("s-games").textContent = state.games.length;
    $("games-count").textContent = state.games.length || "";
    renderGames();
    if (!state.selected && state.games.length) select(state.games[0].fixture_id);
  }

  function renderGames() {
    const el = $("games");
    if (!state.games.length) { el.innerHTML = '<div class="note">no upcoming games priced yet — the ingest fills this live</div>'; return; }
    el.innerHTML = state.games.map((g) => {
      const t = ttj(g.start_time);
      const favTeam = g.favourite === "home" ? g.home : g.favourite === "away" ? g.away : g.favourite;
      return `<div class="grow ${state.selected === g.fixture_id ? "sel" : ""}" data-id="${esc(g.fixture_id)}">
        <div><div class="gname">${esc(g.name)}</div>
        <div class="gsub">${g.sport.toUpperCase()} · <span class="gsrc">${(g.sharp_sources || []).length} sharp · ${g.book_count} books</span>${g.favourite ? ` · <span class="fav">${esc(favTeam || "")} ${g.fav_prob ? (g.fav_prob * 100).toFixed(0) + "%" : ""}</span>` : ""}</div></div>
        <div class="ttj ${t.c}">${t.t}${g.bf_matched ? `<div class="gsrc">${money(g.bf_matched)}</div>` : ""}</div>
      </div>`;
    }).join("");
    el.querySelectorAll(".grow").forEach((x) => x.onclick = () => select(x.dataset.id));
  }

  async function select(id) {
    state.selected = id; sgm.legs = []; sgm.result = null;
    renderGames();
    $("detail").innerHTML = '<div class="empty"><div class="big">◪</div>loading…</div>';
    let d;
    try { d = await (await fetch("/api/game/" + encodeURIComponent(id))).json(); } catch { return; }
    if (d.error) { $("detail").innerHTML = '<div class="empty"><div class="big">◪</div>NO DATA</div>'; return; }
    state.detail = d;
    renderDetail();
  }

  function renderDetail() {
    const d = state.detail; if (!d) return;
    const t = ttj(d.start_time);
    const fair = d.fair || {}, value = d.value || {};
    const sides = ["home", "away", "draw"].filter((s) => s in fair);
    const teamOf = (s) => s === "home" ? d.home : s === "away" ? d.away : "Draw";

    // sharp cards
    const cards = sides.map((s) => `<div class="sharpcard"><div class="side">${esc(teamOf(s) || SIDE[s])}</div>
      <div class="fairodds">${od(value[s] ? value[s].fair_odds : (fair[s] ? 1 / fair[s] : null))}</div>
      <div class="fairp">${(fair[s] * 100).toFixed(1)}% sharp</div></div>`).join("");

    const sharps = d.sharp_sources || [];
    const markets = d.markets || [];
    const selLabel = (m, sel) => m.family === "h2h" ? (teamOf(sel) || SIDE[sel] || sel) : sel.toUpperCase();

    // one row per selection, every market (h2h · totals · spreads · alt lines)
    const marketRows = markets.map((m) => Object.keys(m.fair).map((sel, i) => {
      const v = m.value[sel] || {};
      return `<tr class="${i === 0 ? "mstart" : ""}">
        <td class="mk">${i === 0 ? esc(m.label) : ""}</td>
        <td class="sel">${esc(selLabel(m, sel))}</td>
        <td class="sharp">${od(v.fair_odds || (m.fair[sel] ? 1 / m.fair[sel] : null))}<span class="pp">${(m.fair[sel] * 100).toFixed(0)}%</span></td>
        <td class="best">${v.best_odds ? od(v.best_odds) : "–"}${v.best_book ? ` <span class="bk">${esc(v.best_book)}</span>` : ""}</td>
        <td class="val ${v.value_pct > 0 ? "pos" : "neg"}">${v.value_pct != null ? (v.value_pct > 0 ? "+" : "") + v.value_pct + "%" : "·"}</td>
      </tr>`;
    }).join("")).join("");

    const rating = d.engine_rating;
    $("detail").innerHTML = `
      <div class="dhead"><span class="sport">${d.sport.toUpperCase()}</span><h2>${esc(d.name)}</h2><span class="ttj">${t.t}</span></div>
      <div class="sharpbar">
        ${cards}
        <div><div class="rating" style="margin-bottom:4px">SHARP LINE FROM · ${markets.length} markets</div><div class="srcchips">${sharps.map((s) => `<span class="srcchip">${esc(s)}</span>`).join("") || '<span class="flatc">no sharp priced</span>'}</div></div>
        ${rating ? `<div class="rating">ENGINE RATING<br><b>${(rating.home != null ? (rating.home * 100).toFixed(0) + "% " + esc(d.home) : "")}${rating.away != null ? " · " + (rating.away * 100).toFixed(0) + "% " + esc(d.away) : ""}</b></div>` : ""}
      </div>
      <table class="mkts"><thead><tr><th>MARKET</th><th>SELECTION</th><th>SHARP</th><th>BEST BOOK</th><th>VALUE</th></tr></thead>
      <tbody>${marketRows || '<tr><td colspan="5" class="flatc" style="padding:14px">no priced markets</td></tr>'}</tbody></table>
      ${sgmPanel()}
      <div class="legend">sharp = de-vigged blend of ${sharps.join(" · ") || "—"} over every market · <span class="up">green</span> = best book vs sharp · SGM legs priced by the engine (correlated) or independently when no engine</div>`;
    wireSgm();
  }

  function sgmPanel() {
    const d = state.detail;
    const chips = sgm.legs.map((l, i) => `<span class="sgmchip" data-rm="${i}">${esc(l.label)} @${l.odds.toFixed(2)} ✕</span>`).join("");
    const r = sgm.result;
    let res = '<span class="flatc">add 2+ legs, then generate</span>';
    if (r) {
      if (r.warning) res = `<span class="down">${esc(r.warning)}</span>`;
      else res = `<b class="up">$${(r.fair_odds || 0).toFixed(2)}</b> ${r.priced_by === "engine" ? "engine" : "independent"} · <span class="flatc">${((r.fair_probability || 0) * 100).toFixed(2)}%</span>${r.correlation_lift && r.correlation_lift !== 1 ? ` · corr ×${r.correlation_lift.toFixed(2)}` : ""}`;
    }
    // quick legs from EVERY market's selections (each at its sharp fair odds)
    const quick = (d.markets || []).slice(0, 8).flatMap((m) => Object.keys(m.fair).map((sel) => {
      const v = m.value[sel] || {};
      const o = v.fair_odds || (m.fair[sel] ? 1 / m.fair[sel] : null);
      if (!o || o <= 1) return "";
      const lab = m.family === "h2h" ? (sel === "home" ? d.home : sel === "away" ? d.away : "Draw")
        : `${m.label.replace("Head to Head", "H2H").replace("Total O/U", "O/U")} ${sel}`;
      return `<button class="sgmbtn sgmquick" data-o="${o.toFixed(2)}" data-l="${esc(lab)}">+ ${esc(lab.length > 20 ? lab.slice(0, 20) : lab)}</button>`;
    })).join("");
    return `<div class="sgm">
      <div class="sgmhead">GENERATE SGM PRICE <span class="sub">same-game multi — engine correlated, else independent</span></div>
      <div class="sgmrow">${quick}</div>
      <div class="sgmrow">
        <input class="lbl" id="sgmlbl" placeholder="leg label — e.g. Over 210.5, Player 20+ pts" />
        <input class="odds" id="sgmodds" placeholder="odds" inputmode="decimal" />
        <button class="sgmbtn" id="sgmadd">+ add leg</button>
        <button class="sgmbtn" id="sgmclear">clear</button>
      </div>
      <div class="sgmchips">${chips || '<span class="flatc">add legs…</span>'}</div>
      <div class="sgmrow"><button class="sgmgen" id="sgmgen">⚡ Generate price</button><span class="sgmresult">${res}</span></div>
      ${r && r.warnings && r.warnings.length ? `<div class="sgmnote">${esc(r.warnings[0])}</div>` : ""}
    </div>`;
  }

  function addLeg(label, odds) {
    const o = Number(odds);
    if (!label || !(o > 1)) return;
    sgm.legs.push({ label, odds: o, prob: 1 / o }); sgm.result = null; renderDetail();
  }

  function wireSgm() {
    const root = $("detail");
    root.querySelectorAll(".sgmquick").forEach((b) => b.onclick = () => addLeg(b.dataset.l, b.dataset.o));
    const add = $("sgmadd"); if (add) add.onclick = () => { addLeg($("sgmlbl").value.trim(), $("sgmodds").value); };
    const clr = $("sgmclear"); if (clr) clr.onclick = () => { sgm.legs = []; sgm.result = null; renderDetail(); };
    root.querySelectorAll(".sgmchip").forEach((c) => c.onclick = () => { sgm.legs.splice(+c.dataset.rm, 1); sgm.result = null; renderDetail(); });
    const gen = $("sgmgen"); if (gen) gen.onclick = generate;
  }

  async function generate() {
    const d = state.detail;
    if (sgm.legs.length < 2) { sgm.result = { warning: "a same-game multi needs at least 2 legs" }; return renderDetail(); }
    try {
      sgm.result = await (await fetch("/api/sgm", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sport: d.sport, fixture_id: d.fixture_id, legs: sgm.legs }),
      })).json();
    } catch { sgm.result = { warning: "price service unreachable" }; }
    renderDetail();
  }

  function setConn(ok) {
    const dot = $("conn"), l = $("conn-label");
    dot.className = "dot" + (ok ? " on" : ""); l.textContent = ok ? "LIVE" : "OFFLINE";
  }

  const th = localStorage.getItem("sb-theme");
  if (th) document.documentElement.setAttribute("data-theme", th);
  $("theme").onclick = () => {
    const c = document.documentElement.getAttribute("data-theme") === "light" ? "" : "light";
    if (c) document.documentElement.setAttribute("data-theme", c); else document.documentElement.removeAttribute("data-theme");
    localStorage.setItem("sb-theme", c);
  };
  setInterval(() => { $("clock").textContent = new Date().toLocaleTimeString("en-GB"); }, 1000);
  loadGames();
  setInterval(loadGames, 15000);
})();
