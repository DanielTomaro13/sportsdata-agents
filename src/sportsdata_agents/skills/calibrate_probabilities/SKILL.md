---
name: calibrate_probabilities
description: How to evaluate and fix probability calibration — holdout discipline, Brier/log-loss reading, Platt-style rescaling.
triggers: [calibrate, calibration, brier, log loss, log-loss, overconfident]
---
# Calibrating probabilities

A model that says 70% should be right ~70% of the time. Calibration is measured,
never assumed.

## Measure (always on holdout)

- `calibration_metrics(pairs)` where pairs = holdout `{prob, outcome}` rows.
- **Brier**: mean squared error. 0 = oracle; 0.25 = coin flip on a balanced set;
  beating the market baseline matters more than the absolute number.
- **Log-loss**: punishes confident wrongness. If log-loss looks much worse than
  Brier, the model is overconfident in its tails.

## Fix overconfidence (in run_python)

- Shrink toward the base rate: `p' = w * p + (1 - w) * base_rate`, fit w on a
  validation slice (grid over w ∈ [0.5..1.0] minimising log-loss is fine).
- Platt-style: fit logistic regression of outcome on logit(p) — two parameters,
  hard to overfit; refuse fancier recalibration without more than ~200 samples.
- Re-run `calibration_metrics` AFTER rescaling and report both before/after.

## Persist

`save_model` with the post-calibration metrics and the rescaling parameters in
`params` — the next session must be able to reproduce the pipeline from the row.
