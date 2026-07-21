// RacingBoard Terminal client. One codebase, two sources: live WebSocket or a
// captured replay (GitHub Pages / offline).
(() => {
  const cfg = window.MF_CONFIG || {};
  const qs = new URLSearchParams(location.search);
  const state = { board: [], movers: [], selected: null, details: {}, codeFilter: "ALL", mode: "connecting" };
  const flash = {}; // `${key}:${num}` -> last share, for cell flashing

  const $ = (id) => document.getElementById(id);
  const pct = (x) => (x == null ? "–" : (x * 100).toFixed(1));
  const money = (x) => (x == null ? null : "$" + Math.round(x).toLocaleString());
  const esc = (s) => (s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const BOOK = { pointsbet: "PB", sportsbet: "SB", betfair: "BF", tab: "TAB" };
  function ttg(iso) {
    const m = Math.round((new Date(iso).getTime() - Date.now()) / 60000);
    if (isNaN(m)) return "";
    if (m <= 0) return "NOW";
    if (m < 60) return m + "m";
    return Math.floor(m / 60) + "h" + (m % 60);
  }

  // ---------- data source ----------
  function apply(msg) {
    if (msg.type === "board") {
      state.board = msg.board || [];
      state.movers = msg.movers || [];
      renderTop(); renderTape(); renderBoard(); renderFirmers();
      if (!state.selected && state.board.length) {
        const withPick = state.movers[0] ? state.movers[0].race_key : state.board[0].race_key;
        select(withPick);
      }
    } else if (msg.type === "race") {
      state.details[msg.race_key] = msg.detail;
      if (msg.race_key === state.selected) renderDetail();
    }
  }
  function liveConnect() {
    const base = (cfg.apiBase || (location.protocol === "https:" ? "wss" : "ws") + "://" + location.host).replace(/^http/, "ws");
    let ws, opened = false;
    try { ws = new WebSocket(base + "/ws"); } catch { return startRest(); }
    const ft = setTimeout(() => { if (!opened) { try { ws.close(); } catch {} startRest(); } }, 3500);
    ws.onopen = () => { opened = true; clearTimeout(ft); setMode("live"); };
    ws.onmessage = (e) => apply(JSON.parse(e.data));
    ws.onclose = () => { if (!opened) startRest(); else { setMode("down"); setTimeout(liveConnect, 2500); } };
    window.__sub = (k) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "subscribe", race_key: k })); };
  }
  // REST fallback: some hosts / proxies block WebSockets (e.g. the preview
  // proxy). Poll the same JSON the WS pushes; only if that ALSO fails do we
  // drop to the captured replay (the GitHub Pages case).
  let restOn = false;
  async function startRest() {
    if (restOn) return;
    restOn = true;
    const httpBase = cfg.apiBase ? cfg.apiBase.replace(/^ws/, "http") : "";
    let ok = false;
    window.__sub = (k) => fetch(`${httpBase}/api/race/${encodeURIComponent(k)}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (d) apply({ type: "race", race_key: k, detail: d }); }).catch(() => {});
    async function tick() {
      try {
        const r = await fetch(`${httpBase}/api/board`);
        if (!r.ok) throw new Error("board");
        const d = await r.json();
        if (!ok) { ok = true; setMode("live"); }  // mode "live" so select() REST-fetches detail
        apply({ type: "board", board: d.board, movers: d.movers });
        if (state.selected) window.__sub(state.selected);
      } catch (e) { if (!ok) { restOn = false; startReplay(); } }
    }
    await tick();
    setInterval(tick, 4000);
  }
  async function startReplay() {
    if (state.mode === "replay") return;
    setMode("replay");
    let frames = [];
    try { frames = await (await fetch(qs.get("replay") || cfg.replayUrl || "data/replay.json")).json(); }
    catch { setMode("noreplay"); return; }
    if (!frames.length) { setMode("noreplay"); return; }
    let i = 0;
    window.__sub = (k) => { const f = frames[i % frames.length]; if (f.races && f.races[k]) apply({ type: "race", race_key: k, detail: f.races[k] }); };
    const tick = () => {
      const f = frames[i % frames.length];
      apply({ type: "board", board: f.board, movers: f.movers });
      if (state.selected && f.races && f.races[state.selected]) apply({ type: "race", race_key: state.selected, detail: f.races[state.selected] });
      i++;
    };
    tick(); setInterval(tick, 2600);
  }
  function setMode(m) {
    state.mode = m;
    const d = $("conn"), l = $("conn-label");
    d.className = "dot";
    if (m === "live") { d.classList.add("on"); l.textContent = "LIVE"; }
    else if (m === "replay") { d.classList.add("replay"); l.textContent = "REPLAY"; $("banner").classList.add("show"); }
    else if (m === "down") l.textContent = "RECONNECT";
    else if (m === "noreplay") l.textContent = "NO DATA";
    else l.textContent = "CONNECTING";
  }

  // ---------- top stats ----------
  function renderTop() {
    const b = state.board;
    $("s-races").textContent = b.length || "–";
    $("s-firmers").textContent = state.movers.length || "0";
    const matched = b.reduce((s, r) => s + (r.bf_total_matched || 0), 0);
    $("s-matched").textContent = matched ? money(matched) : "–";
    const next = [...b].filter((r) => r.status === "OPEN").sort((a, z) => new Date(a.start_time) - new Date(z.start_time))[0] || b[0];
    $("s-next").textContent = next ? ttg(next.start_time) : "–";
  }

  // ---------- ticker tape (money in) ----------
  function renderTape() {
    const el = $("tape");
    if (!state.movers.length) { el.innerHTML = `<div class="t"><span class="v">waiting for market moves…</span></div>`; el.style.animation = "none"; return; }
    el.style.animation = "";
    const items = state.movers.map((m) => `
      <div class="t" data-key="${esc(m.race_key)}">
        <span class="d">▲</span><span class="r">${esc(m.runner)}</span>
        <span class="v">${esc(m.venue)} R${m.race_no}</span>
        <span class="d">+${(m.share_delta * 100).toFixed(1)}pt</span>
        ${m.corp_best ? `<span class="v">$${m.corp_best.toFixed(2)}</span>` : ""}
      </div>`).join("");
    el.innerHTML = items + items; // duplicate for seamless loop
    el.querySelectorAll(".t[data-key]").forEach((t) => t.onclick = () => select(t.dataset.key));
  }

  // ---------- races board ----------
  function renderBoard() {
    const el = $("board");
    const rows = state.board.filter((r) => state.codeFilter === "ALL" || r.code === state.codeFilter);
    $("board-count").textContent = rows.length || "";
    if (!rows.length) { el.innerHTML = `<div class="brow"><span class="flatc mono">waiting…</span></div>`; return; }
    el.innerHTML = rows.map((r) => {
      const p = r.pick;
      const soon = (new Date(r.start_time) - Date.now()) < 5 * 60000;
      const pickTxt = p
        ? `<span class="pn">#${p.number} ${esc(p.name)}</span>${p.direction === "firming" ? ` <span class="pd">▲${((p.share_delta || 0) * 100).toFixed(0)}pt</span>` : ` <span class="flatc">${esc(p.confidence)}</span>`}`
        : "";
      return `
      <div class="brow ${r.race_key === state.selected ? "sel" : ""}" data-key="${esc(r.race_key)}">
        <span class="code ${r.code}">${r.code}</span>
        <span class="rv-wrap" style="min-width:0">
          <div class="rv"><span class="venue">${esc(r.venue)}</span><span class="rno">R${r.race_no}</span>${r.has_betfair ? '<span class="bf">BF</span>' : ""}</div>
          <div class="pick">${pickTxt}</div>
        </span>
        <span class="rt"><div class="ttg ${soon ? "soon" : ""}">${ttg(r.start_time)}</div><div class="st">${r.status !== "OPEN" ? esc(r.status) : ""}</div></span>
      </div>`;
    }).join("");
    el.querySelectorAll(".brow[data-key]").forEach((x) => x.onclick = () => select(x.dataset.key));
  }

  // ---------- firmers ----------
  function renderFirmers() {
    const el = $("firmers");
    if (!state.movers.length) { el.innerHTML = `<div class="frow"><span></span><span class="who flatc">no shorteners yet…</span><span></span><span></span></div>`; return; }
    el.innerHTML = state.movers.map((m) => {
      const v = m.value_pct;
      return `
      <div class="frow" data-key="${esc(m.race_key)}" data-tip="mover" data-json='${esc(JSON.stringify(m))}'>
        <span class="ar">▲</span>
        <span class="who"><div class="n">${esc(m.runner)}</div><div class="c"><span class="code ${m.code}">${m.code}</span> ${esc(m.venue)} R${m.race_no}</div></span>
        <span class="d">+${(m.share_delta * 100).toFixed(1)}</span>
        <span class="v ${v > 0 ? "pos" : "neg"}">${v != null ? (v > 0 ? "+" : "") + v.toFixed(0) + "%" : ""}</span>
      </div>`;
    }).join("");
    el.querySelectorAll(".frow[data-key]").forEach((x) => x.onclick = () => select(x.dataset.key));
    wireTips(el);
  }

  // ---------- detail ----------
  function select(k) {
    state.selected = k;
    if (window.__sub) window.__sub(k);
    if (state.details[k]) renderDetail();
    else if ((state.mode === "live" || state.mode === "down") && !cfg.apiBase)
      fetch(`/api/race/${encodeURIComponent(k)}`).then((r) => r.ok ? r.json() : null).then((d) => { if (d) { state.details[k] = d; renderDetail(); } });
    renderBoard();
  }

  function renderDetail() {
    const d = state.details[state.selected];
    const el = $("detail");
    if (!d) { el.innerHTML = `<div class="empty"><div class="big">▟</div>NO DATA FOR THIS RACE</div>`; return; }
    const ref = d.ref, p = d.pick;
    const runners = d.runners.filter((r) => !r.scratched);
    const maxShare = Math.max(0.001, ...runners.map((r) => r.tote_pool_share || 0));
    const pickNum = p ? p.number : -1;

    el.innerHTML = `
      <div class="dhead">
        <span class="code ${ref.code}">${ref.code}</span>
        <h2>${esc(ref.venue)} <span class="rno">R${ref.race_no}</span></h2>
        <span class="st ${d.status === "OPEN" ? "open" : ""}">${esc(d.status)}</span>
      </div>
      <div class="meta">
        <div class="m"><div class="k">JUMP</div><div class="v ${(new Date(ref.start_time) - Date.now()) < 3e5 ? "up" : ""}">${ttg(ref.start_time)}</div></div>
        <div class="m"><div class="k">TOTE WIN POOL</div><div class="v">${money(d.tote_win_pool) || "<span class='flatc'>forming</span>"}</div></div>
        <div class="m"><div class="k">BETFAIR MATCHED</div><div class="v">${money(d.bf_total_matched) || (ref.betfair_market_id ? "…" : "n/a")}</div></div>
        <div class="m"><div class="k">RUNNERS</div><div class="v">${runners.length}</div></div>
      </div>
      ${p ? pickCard(p) : ""}
      <div class="grid">
        <div class="ghead"><span>#</span><span>RUNNER</span><span class="r">SHARE</span><span class="r">Δ IN</span><span class="r">FAIR</span><span class="r">BEST</span><span class="r">VAL</span><span class="r">BF</span><span class="r">TREND</span></div>
        ${runners.map((r) => grow(r, maxShare, pickNum)).join("")}
      </div>
      ${pricePanel(runners)}
      <div class="legend"><b>▲ money in</b> = tote pool share rising / price shortening · FAIR = <span class="eng">E</span> sportsdata racing engine, else de-vigged Betfair·tote · VAL = best book vs fair · cells flash on change</div>`;

    el.querySelectorAll("canvas.spark").forEach(drawSpark);
    wireTips(el);
  }

  // ---------- exotics & same-race-multi price generator (client-side, mirrors
  // quant/exotics.py — Harville closed form for exotics, Plackett-Luce Monte
  // Carlo for SRM; driven by the runner FAIR prices already on the board, so
  // it uses the sportsdata racing engine's win probs when they're present) ----
  const pricer = { bet: "exacta", picks: [], legs: [], box: false, result: null };
  const BANDS = { win: 1, top2: 2, top3: 3, top4: 4 };
  const EXOTIC_N = { exacta: 2, quinella: 2, trifecta: 3, first4: 4 };

  function winProbs(runners) {
    const p = {};
    let tot = 0;
    for (const r of runners) {
      if (r.fair_price && r.fair_price > 1) { p[r.number] = 1 / r.fair_price; tot += p[r.number]; }
    }
    for (const k in p) p[k] /= tot || 1;
    return p;
  }
  function orderedProb(probs, seq) {
    let rem = 1, pr = 1;
    for (const n of seq) { const q = probs[n]; if (!q || rem <= 0) return 0; pr *= q / rem; rem -= q; }
    return pr;
  }
  function perms(arr, k) {
    if (k === 0) return [[]];
    const out = [];
    arr.forEach((x, i) => { for (const rest of perms(arr.slice(0, i).concat(arr.slice(i + 1)), k - 1)) out.push([x, ...rest]); });
    return out;
  }
  function priceExotic(probs, bet, picks, box) {
    const need = EXOTIC_N[bet];
    if (picks.length < need) return { warning: `needs ${need} runners` };
    let prob = 0, combos = 1;
    if (bet === "quinella") { prob = orderedProb(probs, [picks[0], picks[1]]) + orderedProb(probs, [picks[1], picks[0]]); if (box) { const ps = perms(picks, 2); prob = ps.reduce((s, q) => s + orderedProb(probs, q), 0); combos = picks.length * (picks.length - 1) / 2; } }
    else if (box) { const ps = perms(picks, need); prob = ps.reduce((s, q) => s + orderedProb(probs, q), 0); combos = ps.length; }
    else prob = orderedProb(probs, picks.slice(0, need));
    return prob > 0 ? { prob, fair: 1 / prob, combos } : { warning: "no chance / unpriced" };
  }
  function priceSRM(probs, legs, sims = 8000) {
    if (legs.length < 2) return { warning: "2+ legs" };
    if (legs.filter((l) => BANDS[l.position] === 1).length > 1) return { prob: 0, fair: null, warning: "two can't both win" };
    const nums = Object.keys(probs).map(Number), depth = Math.max(...legs.map((l) => BANDS[l.position]));
    let hits = 0;
    for (let s = 0; s < sims; s++) {
      const pool = nums.slice(), w = pool.map((n) => probs[n]), pos = {};
      for (let d = 0; d < depth && pool.length; d++) {
        let tot = 0; for (const x of w) tot += x; let x = Math.random() * tot, idx = 0;
        for (; idx < w.length; idx++) { x -= w[idx]; if (x <= 0) break; }
        idx = Math.min(idx, pool.length - 1); pos[pool[idx]] = d + 1; pool.splice(idx, 1); w.splice(idx, 1);
      }
      if (legs.every((l) => (pos[l.runner] || 99) <= BANDS[l.position])) hits++;
    }
    const prob = hits / sims;
    return prob > 0 ? { prob, fair: 1 / prob, sims } : { warning: "no simulated combos landed" };
  }

  function pricePanel(runners) {
    const isSrm = pricer.bet === "srm";
    const opts = runners.map((r) => `<option value="${r.number}">${r.number} · ${esc(r.name)}</option>`).join("");
    const chips = isSrm
      ? pricer.legs.map((l, i) => `<span class="pxchip" data-rm="${i}">#${l.runner} ${l.position} ✕</span>`).join("")
      : pricer.picks.map((n, i) => `<span class="pxchip" data-rm="${i}">${i ? "→ " : ""}#${n} ✕</span>`).join("");
    const res = pricer.result;
    let resHtml = '<span class="flatc">build a bet, then generate</span>';
    if (res) {
      if (res.warning) resHtml = `<span class="down">${esc(res.warning)}</span>`;
      else resHtml = `<b class="up">$${res.fair.toFixed(2)}</b> fair · <span class="flatc">${(res.prob * 100).toFixed(res.prob < 0.01 ? 3 : 2)}%</span>${res.combos > 1 ? ` · ${res.combos} combos` : ""}${res.sims ? ` · ${res.sims} sims` : ""}`;
    }
    return `<div class="pricer">
      <div class="pxhead">GENERATE PRICE <span class="sub">exotics &amp; same-race multi — off the board's fair prices</span></div>
      <div class="pxrow">
        ${["exacta", "quinella", "trifecta", "first4", "srm"].map((b) => `<button class="pxbet ${pricer.bet === b ? "on" : ""}" data-bet="${b}">${b.toUpperCase()}</button>`).join("")}
        ${isSrm ? "" : `<label class="pxbox"><input type="checkbox" id="pxbox" ${pricer.box ? "checked" : ""}> box</label>`}
      </div>
      <div class="pxrow">
        <select id="pxrunner">${opts}</select>
        ${isSrm ? `<select id="pxpos"><option value="win">win</option><option value="top2">top-2</option><option value="top3" selected>top-3</option><option value="top4">top-4</option></select>` : ""}
        <button class="pxadd" id="pxadd">+ add</button>
        <button class="pxclear" id="pxclear">clear</button>
      </div>
      <div class="pxchips">${chips || '<span class="flatc">pick runners…</span>'}</div>
      <div class="pxrow"><button class="pxgen" id="pxgen">⚡ Generate price</button><span class="pxresult">${resHtml}</span></div>
    </div>`;
  }

  function computePrice() {
    const d = state.details[state.selected];
    if (!d) return;
    const probs = winProbs(d.runners.filter((r) => !r.scratched));
    pricer.result = pricer.bet === "srm"
      ? priceSRM(probs, pricer.legs)
      : priceExotic(probs, pricer.bet, pricer.picks, pricer.box);
    renderDetail();
  }

  function pickCard(p) {
    const dv = (p.share_delta || 0) * 100;
    const why = p.reason === "money in"
      ? `<span class="conf">${esc(p.confidence)}</span> · money in ▲${dv.toFixed(0)}pt${p.price_move_pct != null ? ` · price ${p.price_move_pct.toFixed(0)}%` : ""}`
      : `<span class="conf">${esc(p.confidence)}</span> · market favourite`;
    return `
      <div class="pickcard">
        <span class="tag">PICK</span>
        <div class="who"><div class="n"><span class="sn">#${p.number}</span>${esc(p.name)}</div><div class="why">${why}</div></div>
        <div class="nums">
          <div class="c"><div class="k">SHARE</div><div class="val">${pct(p.share)}%</div></div>
          <div class="c"><div class="k">FAIR</div><div class="val">${p.fair_price ? p.fair_price.toFixed(2) : "–"}</div></div>
          <div class="c"><div class="k">BEST</div><div class="val up">${p.corp_best ? p.corp_best.toFixed(2) : "–"}</div></div>
        </div>
      </div>`;
  }

  function grow(r, maxShare, pickNum) {
    const key = state.selected + ":" + r.number;
    const share = r.tote_pool_share || 0;
    const prev = flash[key];
    flash[key] = share;
    const fl = prev != null && Math.abs(share - prev) > 0.001 ? (share > prev ? "fUp" : "fDn") : "";
    const barW = (share / maxShare) * 100;
    const dv = r.share_delta != null ? r.share_delta * 100 : null;
    const val = r.value_pct;
    return `
      <div class="grow ${r.direction === "firming" ? "firm" : ""} ${r.number === pickNum ? "isPick" : ""} ${fl}" data-tip="runner" data-json='${esc(JSON.stringify(r))}'>
        <span class="num">${r.number}</span>
        <span class="nm">${esc(r.name)} ${r.direction === "firming" ? '<span class="up">▲</span>' : ""}</span>
        <span class="r share">${pct(share)}<span class="bar" style="width:${barW}%"></span></span>
        <span class="r delta ${dv > 0.5 ? "up" : "flatc"}">${dv != null && dv > 0.5 ? "+" + dv.toFixed(0) : "·"}</span>
        <span class="r">${r.fair_price ? r.fair_price.toFixed(2) : "–"}${r.fair_source === "engine" ? '<span class="eng" title="sportsdata racing engine fair">E</span>' : ""}</span>
        <span class="r best">${r.corp_best ? r.corp_best.toFixed(2) : "–"}${r.corp_best_book ? ` <span class="bk">${BOOK[r.corp_best_book] || ""}</span>` : ""}</span>
        <span class="r val ${val > 0 ? "pos" : "neg"}">${val != null ? (val > 0 ? "+" : "") + val.toFixed(0) : "·"}</span>
        <span class="r bf">${r.bf_back ? r.bf_back.toFixed(1) : "–"}</span>
        <canvas class="spark" width="68" height="20" data-points='${esc(JSON.stringify(r.share_spark || []))}' data-dir="${r.direction}"></canvas>
      </div>`;
  }

  function drawSpark(c) {
    const pts = JSON.parse(c.dataset.points || "[]").filter((v) => v != null);
    const ctx = c.getContext("2d"), W = c.width, H = c.height, pad = 2;
    ctx.clearRect(0, 0, W, H);
    if (pts.length < 2) return;
    const mn = Math.min(...pts), mx = Math.max(...pts), rg = (mx - mn) || 1;
    const col = c.dataset.dir === "firming" ? "#21d16b" : c.dataset.dir === "drifting" ? "#ff4d4f" : "#6a6a76";
    const X = (i) => pad + (i / (pts.length - 1)) * (W - 2 * pad);
    const Y = (v) => H - pad - ((v - mn) / rg) * (H - 2 * pad);
    ctx.beginPath(); pts.forEach((v, i) => i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v)));
    ctx.strokeStyle = col; ctx.lineWidth = 1.4; ctx.stroke();
  }

  // ---------- tooltip ----------
  const tt = $("tt");
  function wireTips(root) {
    root.querySelectorAll("[data-tip]").forEach((el) => {
      el.onmousemove = (e) => showTip(e, el.dataset.tip, JSON.parse(el.dataset.json));
      el.onmouseleave = () => tt.classList.remove("show");
    });
  }
  function showTip(e, kind, j) {
    let h;
    if (kind === "mover") {
      h = `<div class="tt-t">${esc(j.runner)}</div>
        <div class="tt-r"><span>MONEY IN</span><b class="up">+${(j.share_delta * 100).toFixed(1)}pt</b></div>
        <div class="tt-r"><span>SHARE</span><b>${pct(j.share)}%</b></div>
        <div class="tt-r"><span>PRICE MOVE</span><b>${j.price_move_pct != null ? j.price_move_pct.toFixed(0) + "%" : "–"}</b></div>
        <div class="tt-r"><span>FAIR / BEST</span><b>${j.fair_price ? j.fair_price.toFixed(2) : "–"} / ${j.corp_best ? j.corp_best.toFixed(2) : "–"}</b></div>
        <div class="tt-r"><span>RACE</span><b>${esc(j.venue)} R${j.race_no}</b></div>`;
    } else {
      const corp = j.corp || {};
      const rows = Object.entries(corp).sort((a, z) => z[1] - a[1]).map(([b, px]) => `<div class="tt-r"><span>${BOOK[b] || b}${b === j.corp_best_book ? " ★" : ""}</span><b>${px.toFixed(2)}</b></div>`).join("");
      h = `<div class="tt-t">#${j.number} ${esc(j.name)}</div>
        <div class="tt-r"><span>POOL SHARE</span><b>${pct(j.tote_pool_share)}%</b></div>
        <div class="tt-r"><span>MONEY IN</span><b class="${j.direction === "firming" ? "up" : "flatc"}">${j.share_delta != null ? (j.share_delta > 0 ? "+" : "") + (j.share_delta * 100).toFixed(1) + "pt" : "–"}</b></div>
        <div class="tt-r"><span>FAIR</span><b>${j.fair_price ? j.fair_price.toFixed(2) : "–"}</b></div>
        <div class="tt-r"><span>VALUE</span><b class="${j.value_pct > 0 ? "up" : ""}">${j.value_pct != null ? (j.value_pct > 0 ? "+" : "") + j.value_pct + "%" : "–"}</b></div>
        ${j.bf_back != null ? `<div class="tt-r"><span>BETFAIR B/L</span><b>${j.bf_back} / ${j.bf_lay ?? "–"}</b></div>` : ""}
        ${j.bf_wom != null ? `<div class="tt-r"><span>WEIGHT OF $</span><b>${(j.bf_wom * 100).toFixed(0)}% back</b></div>` : ""}
        <div class="tt-r"><span>TOTE / TAB FIX</span><b>${j.tote_win ? j.tote_win.toFixed(2) : "–"} / ${j.fixed_win ? j.fixed_win.toFixed(2) : "–"}</b></div>
        ${rows ? `<div class="tt-sep">FIXED ODDS</div>${rows}` : ""}`;
    }
    tt.innerHTML = h; tt.classList.add("show");
    const w = tt.offsetWidth, ht = tt.offsetHeight;
    let x = e.clientX + 14, y = e.clientY + 14;
    if (x + w > innerWidth) x = e.clientX - w - 14;
    if (y + ht > innerHeight) y = e.clientY - ht - 14;
    tt.style.left = x + "px"; tt.style.top = y + "px";
  }

  // ---------- chrome ----------
  $("code-filters").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    state.codeFilter = b.dataset.code;
    document.querySelectorAll("#code-filters button").forEach((x) => x.classList.toggle("active", x === b));
    renderBoard();
  });
  const th = localStorage.getItem("mf-theme");
  if (th) document.documentElement.setAttribute("data-theme", th);
  $("theme").onclick = () => {
    const c = document.documentElement.getAttribute("data-theme") === "light" ? "" : "light";
    if (c) document.documentElement.setAttribute("data-theme", c); else document.documentElement.removeAttribute("data-theme");
    localStorage.setItem("mf-theme", c);
    if (state.selected) renderDetail();
  };
  setInterval(() => {
    $("clock").textContent = new Date().toLocaleTimeString("en-GB");
    renderTop(); renderBoard();
  }, 1000);

  // price generator — one delegated listener on the (persistent) #detail node,
  // so it survives the detail panel's re-render every poll without losing state
  $("detail").addEventListener("click", (e) => {
    const bet = e.target.closest(".pxbet");
    if (bet) { pricer.bet = bet.dataset.bet; pricer.picks = []; pricer.legs = []; pricer.result = null; return renderDetail(); }
    if (e.target.closest("#pxadd")) {
      const num = Number($("pxrunner").value);
      if (pricer.bet === "srm") { if (!pricer.legs.some((l) => l.runner === num)) pricer.legs.push({ runner: num, position: $("pxpos").value }); }
      else if (!pricer.picks.includes(num)) pricer.picks.push(num);
      return renderDetail();
    }
    const rm = e.target.closest(".pxchip");
    if (rm) { const i = Number(rm.dataset.rm); (pricer.bet === "srm" ? pricer.legs : pricer.picks).splice(i, 1); pricer.result = null; return renderDetail(); }
    if (e.target.closest("#pxclear")) { pricer.picks = []; pricer.legs = []; pricer.result = null; return renderDetail(); }
    if (e.target.closest("#pxgen")) return computePrice();
  });
  $("detail").addEventListener("change", (e) => {
    if (e.target.id === "pxbox") { pricer.box = e.target.checked; if (pricer.result) computePrice(); }
  });

  const api = qs.get("api") || cfg.apiBase;
  if (api) { cfg.apiBase = api; liveConnect(); }
  else if (cfg.forceReplay) startReplay();
  else liveConnect();
})();
