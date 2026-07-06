# Roadmap & deferred register

The single place tracking what's shipped, what's parked, and what gates what.
Updated 2026-07-06 (agents v0.47.0).

## Live and self-running (nothing required)

- **Capture**: 18 sources, parallel fetch, monitor + racing feeds at 60s, all
  six AU racing books speaking one race identity, Melbourne-local times.
- **Alerts** (11 watch kinds): arb, line_move, steam, value, scratching,
  model_value, exchange_value, stat_value, racing_value, prediction_value,
  back_lay. Each carries Kelly stake, price age, and (where relevant) matched
  money; all params per-watch tunable via `update_watch`.
- **Settlement + scoreboard**: racing (fixture-join), prediction (Kalshi/
  Polymarket resolutions), exchange/model h2h (fixture winner) all settle;
  weekly `scoreboard --push` + daily `digest --push`; auto-tuning suggestions
  from settled ROI.
- **Retention**: 12GB size budget + batched prune + WAL truncate + 1 backup.
- **Form**: TAB authenticated form captured half-hourly into `race_form`.
- **Measurement trail**: engine predictions recorded across all sports; CLV vs
  Betfair closes; results settled nightly.

## Blocked only on the measurement clock (~July 20)

Two weeks of clean data from the 2026-07-06 fresh start. NOT startable earlier —
they need the sample.

1. **Phase B accuracy fits** (impact order): pace/correlation activation →
   per-sport dispersion → NFL key numbers → per-book margin curves (upgrades
   racing alerts from pack-outliers to trustworthy mid-price signals).
2. **Form-powered racing ratings**: fit barrier/weight/freshness from `race_form`
   against actual finishes (data accumulating from 2026-07-06).
3. **EDGE-VERDICT re-issue**: the measured yes/no on persistent edge. Gates
   everything below.

## Needs ~20 minutes of the operator

4. **SGM traffic capture** — see `SGM_CAPTURE.md`. The engine side is done
   (`engine_sgm_quote`, correlation + h2h-draw rules); only the capture surface
   is missing. Unlocks `sgm_value` on the softest market class.

## Gated on the EDGE-VERDICT (green = go)

5. **Deploy hosted engine API + paid tier** — code-complete (engines Phase 9/10:
   FastAPI, auth, metering, storefronts). Do NOT deploy a paid product before
   the verdict proves edge.
6. **Autobet paper-trading mode** — record "would-have-placed" bets at alert
   time with real stake sizing → a true simulated bankroll curve. Small build;
   the scoreboard's alert history already has everything needed. Can start
   pre-verdict as a measurement aid.

## Scale / infra (do when the metric bites)

7. **Postgres/Timescale migration** — tooling exists (`agents migrate`,
   Timescale hypertable path). Trigger: SQLite write contention or the size
   budget forcing too-short retention. Not needed at 385MB.
8. **More books/exchanges** — Betfair UK/international beyond racing;
   Ladbrokes/Neds fuller coverage via Entain; US books if wanted. Each is a
   spec + fetcher + normalizer (~half a day per book).

## Small confirmed-terms / hardening backlog

- Sportsbet dead-heat stake-floor rule; PointsBet NFL tie=push per-sport override.
- Self-hosted ntfy (alert content currently transits ntfy.sh).
- stat_value settlement (needs a player-stat actuals source — no results feed
  yet; the only alert kind still counted-not-settled).
- Betfair racing lay-side already captured; back_lay scan now uses it.

## Deferred with rationale (decided NOT to do)

- Twitter/X as a priced feed — it's a research surface (`social.*` serves
  agents directly), nothing to warehouse.
- FanDuel flagging in AU value scans — unbettable from AU; it feeds consensus
  but is never flagged (exclude_books default).
- Aggregator odds (nba_cdn) — we capture books directly; kept only for fixture
  parity.
