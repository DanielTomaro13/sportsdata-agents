---
name: build_a_totals_model
description: Recipe for a simple, calibrated totals (over/under) model — data prep, a normal-approximation baseline, holdout evaluation.
triggers: [totals model, over/under model, totals, build a model, model the total]
---
# Build a totals model (the boring, calibrated way)

A totals model predicts P(total points > line). The baseline that is hard to beat:
model the total as Normal(μ, σ) with μ/σ estimated from recent pace-adjusted games.

## Recipe (run_python, one script)

1. **Data**: per-game totals for both teams, most recent N games (N=20 is plenty).
   Fetch via your data tools; print the rows you keep. Never fabricate games.
2. **Estimate**: μ = weighted mean of (team_total + opp_total) with recency weights
   (e.g. exponential, half-life ~10 games); σ = weighted std, floor it at 8 points.
3. **Probability**: `p_over = 1 - norm.cdf(line, mu, sigma)` (scipy or a hand-rolled
   CDF — `0.5 * (1 + math.erf((x - mu) / (sigma * 2**0.5)))`).
4. **Holdout**: fit on games [0..k), evaluate on [k..n). Collect {prob, outcome}
   pairs from the holdout ONLY.
5. **Calibrate + persist**: `calibration_metrics(pairs)` → pass its exact output to
   `save_model` (params: μ/σ method, weights, N, the line convention). Then
   `record_predictions` for upcoming games.

## Honesty rules

- Print every number; quote only printed numbers.
- A Brier ≥ 0.25 means the model is no better than a coin flip on a balanced set —
  say so plainly rather than dressing it up.
- Compare against the market when the warehouse has the line (`query_line_movement`):
  beating the closing line is the bar that matters (§16.3 CLV).
