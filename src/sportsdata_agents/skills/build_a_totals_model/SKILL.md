---
name: build_a_totals_model
description: Worked example of model_development for totals (over/under) markets — scoring-process baseline, pace adjustment, holdout evaluation.
triggers: [totals model, over/under model, total points model, model the total]
---
# Build a totals model — worked example

Follow `model_development` for the method; this is the totals instantiation.

## Recipe (run_python, one script)

1. **Data**: per-game totals for both teams across as many seasons as the regime
   allows — count EVENTS against the parameter budget (model_development §2: 10–20
   outcomes per parameter; a recent-window-only model must say how little it knows).
   Weight recency (exponential decay, half-life tuned on train) rather than
   truncating to a tiny window.
2. **Model the scoring process**: totals are sums of scoring events —
   Normal(μ, σ) for high-scoring sports (basketball), Poisson for low-scoring
   (soccer/NHL goals). μ from pace-adjusted team offense/defense; σ estimated, not
   assumed (floor it sensibly; print it).
3. **Probability**: `p_over = 1 - CDF(line)` — hand-rolled normal CDF
   (`0.5 * (1 + math.erf((x - mu) / (sigma * 2**0.5)))`) keeps the sandbox
   dependency-free.
4. **Features beyond pace** (ask the user which they value — model_development §3):
   rest days, altitude/venue, weather for outdoor sports. Each one is a parameter;
   justify it.
5. **Holdout**: train on the earlier slice, collect {prob, outcome} on the later
   slice ONLY, `calibration_metrics` → `save_model` (params: μ/σ method, weights,
   seasons, line convention) → `record_predictions` with honest `predicted_at`.

## Honesty rules

- A Brier ≥ 0.25 on a balanced set is coin-flip territory — say so plainly.
- The bar is the closing total (`query_line_movement`), not the base rate
  (quant_concepts: CLV) — report model vs market side by side.
