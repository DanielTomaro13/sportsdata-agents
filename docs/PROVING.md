# The proving plan (paper-track)

Set 2026-07-07. Objective for the next 90 days: **prove the edge before anything
else** — no real money during the proving window, no monetisation moves until the
verdict says the signals pay. The system already contains its own honesty
mechanisms (alert settlement, the P&L scoreboard, the engines replay harness,
EDGE-VERDICT); this plan just sequences them.

## Ground rules

- **Paper-track only.** Alerts are recorded and settled; nothing is staked.
- **Thresholds frozen** until the first data-driven review — a threshold changed
  mid-window contaminates the sample.
- Signals in scope (all live as of v0.68.0): `racing_value` (Betfair/engine
  shorter, ≥$1k matched, jump ≤60m), `bsp_value`, `model_value` × 12 sports,
  `value` (engine-edges-extra), `exchange_value` (Betfair), `sharp-value-pinnacle`,
  `arb`, `back_lay`, `props-value`, `prediction_value`.

## Phase 1 — instrument the paper-track (week of 7 Jul)

Paper-tracking makes the settlement loop the whole experiment, and it has gaps:

1. ~~**Grade every value kind.**~~ DONE 2026-07-07: `model_value`/`value`
   settle against result scores (h2h/totals/lines, pushes returned) at a
   flat $1; `bsp_value` settles like racing; `stat_value` still awaits
   player actuals and the report says so.
2. ~~**Record closing-line value.**~~ DONE 2026-07-07: computed at settlement
   from the prices table (last change before the start, same book/market/
   selection) — mean CLV per kind in the scoreboard, with a tuning warning
   when 20+ samples run negative.
3. **Place-market alerts.** Racing `model_value` with explicit `places` terms
   (TAB first) — the scan already finds corroborated place edges that never
   reach Discord.

`agents replay-export` (2026-07-07) ships settled fixtures as ReplayFixture
JSONL for Phase 3 (verified round-tripping through the engines harness).

## Phase 2 — accumulate (7–21 Jul)

Hands off. The scheduler runs; the warehouse fills; alerts settle.
A scheduled **Monday 09:00 scoreboard review** reports: alerts by kind,
settled P&L, CLV, hit-rate vs implied, and the registry's tuning suggestions —
*reported, not applied*.

## Phase 3 — replay + verdict (week of 21 Jul)

A one-shot scheduled checkpoint (21 Jul 09:00):

1. Run the engines **replay harness at scale** against two weeks of warehouse
   captures; re-fit per-sport dispersion/pace in `data/*.json`.
2. **Re-issue EDGE-VERDICT** from measured hit-rate, flat-stake ROI and CLV per
   sport/signal.
3. **Tune watch thresholds** from settled outcomes (the scoreboard's
   suggestions, now with a real sample behind them).

## Phase 4 — decide from evidence (late Jul)

- **Verdict green** (signals beat the close): build the portfolio/staking layer
  (daily risk budget, correlated-exposure limits, error-aware kelly via the
  engines staking seam), move to real money at small size, and *then* consider
  the commercial surfaces (hosted engines API, MCP premium tier — machinery
  already built, deliberately dormant).
- **Verdict amber/red**: keep the sharp-corroborated signals (they don't depend
  on the engine being right), iterate the models on replay data, re-run Phase 3
  in two-week cycles.

## The work queue (updated 2026-07-07 evening)

**Track 1 — measurement (the priority; everything downstream consumes it):**

1. **Grade every alert kind** in the scoreboard against ``event_results`` —
   h2h from winners, totals/lines from ``meta.score``, racing win/place from
   result selections. Until this lands the verdict can only score racing and
   arbs. (~half day)
2. **Closing-line stamps + CLV** — a post-start pass writes each alert's
   closing price; the scoreboard reports CLV per MODEL FAMILY (calibrated vs
   stats vs sharp-fair). CLV separates the families in days, not weeks.
   (~half day)
3. **Cross-book prop value scan** — group prop-tagged rows by (player, stat,
   line), de-vig each book's over/under pair, flag books above the industry
   fair. Land before Thursday's AFL round. (~1 day)
4. **Results coverage** for basketball/WNBA/NHL (widen the ESPN sweep) so
   those alerts settle. (hours)
5. **Replay-export CLI** (``agents replay-export`` → engines ReplayFixture
   JSON) before the 21 Jul checkpoint. (~half day)

**Track 2 — product/UI (after Track 1 items 1–2):** create-watch flow seeded
from registry defaults; per-knob form controls replacing the JSON editor;
Discord channel-split routing (webhooks → ``discord:ENV`` per family); alerts
pane filters + settled-P&L column (Monitors becomes the paper-track
dashboard); server-driven coverage catalogue; stale Settings header copy.

**Track 3 — standing automation (running, nothing to do):** daily 08:30
mapping audit (8 checks incl. stats-vs-market sanity), Monday 09:00
scoreboard review, 21 Jul replay + verdict one-shot. Thresholds stay frozen.

## Reviewed backlog (triple-review 2026-07-07 — verified findings, not yet applied)

- ~~Ratings margin-band gate~~ covered 2026-07-07 by the h2h sanity gate (a
  +78 margin implies an h2h prob far outside the 15-point band); the per-team
  effective-appearance floor in the footy fit remains open.
- ~~Racing exchange-vs-pack cross-check~~ DONE 2026-07-07: a runner whose
  exchange fair reads 1.5x the pack's median probability prices against the
  pack instead ("exchange back distrusted").
- Form-slate dedupe key needs the venue (same race number + same minute at
  two tracks currently drops one race).
- ~~Provider-scope the event lookups~~ DONE 2026-07-07 (value watch keys by
  book label; ratings anchors by feed).
- ~~Scoreboard bucket/odds/arb-credit fixes~~ DONE 2026-07-07 with the
  grading work (no_exchange bucket, odds ≤ 1.0 pending, re-measured margin).
- Scheduler calendar jobs may dispatch in LOCAL time against a UTC-authored
  table (digest/scoreboard hours) — verify and normalise; missed calendar
  slots have no catch-up; `_line_suffix` collapses a genuine 0.0 line.
- Gateway: default the mutation token on; try/except the /watches DB session
  (503 contract). MCP: per-provider rate limits are per-process (N subprocesses
  multiply pressure); consider a shared budget if blocks recur.
- model_value band suppression has no vanished-kind reset (an edge that dies
  and re-emerges at the same band stays silent for the fixture's life) —
  mirror the value watch's Edge-Gone if re-emergence should be news.
- Repair sweep: consider a rapid-repeat time guard beyond baseball (esoccer /
  table-tennis same-pairing rematches) once those sports' fixture start
  stamps are trustworthy; today the ambiguity skip is the only shield.

## Standing follow-ups (not gated on the verdict)

- Entain/Unibet persisted-hash auto-refresh hardening (MCP).
- Deeper per-event routes for PointsBet/Dabble analogous to BetR's
  `GroupTypeCode` find.
- In-play remains a research branch — blocked on data-plane terms, engines
  already price it.
