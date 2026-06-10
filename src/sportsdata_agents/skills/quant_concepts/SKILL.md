---
name: quant_concepts
description: Working definitions of the core quant concepts — Brier, log-loss, calibration, logistic regression, gradient boosting/XGBoost, regularization, walk-forward CV, CLV — and when each tool fits.
triggers: [brier, log loss, log-loss, logistic regression, xgboost, gradient boosting, overfit, overfitting, calibration curve, regularization, walk-forward]
---
# Quant concepts — working definitions

Use these to explain your choices and to choose the right tool for the data volume.

## Scoring probabilities

- **Brier score**: mean squared error of prob vs outcome. 0 = oracle; 0.25 = coin
  flip on a balanced set. Insensitive to tail confidence; easy to interpret.
- **Log-loss**: negative mean log-likelihood. Punishes confident wrongness brutally
  (a 0.99 that loses costs ~4.6; a 0.6 that loses costs ~0.9). If log-loss looks
  much worse than Brier, the model is overconfident in its tails.
- **Calibration curve**: bucket predictions (e.g. deciles), compare bucket mean prob
  vs bucket hit rate. The diagonal is calibrated; S-shapes mean over/underconfidence.
  With < ~200 samples, buckets are noise — say so instead of plotting noise.
- Always score on out-of-sample data; training-set scores are advertising.

## Model families (match the tool to the sample size)

- **Logistic regression** — the default. Linear in log-odds, a handful of
  parameters, stable on hundreds of events, coefficients are readable ("home
  advantage = +0.18 log-odds"). Add L2 regularization when features correlate.
- **Poisson / normal approximations** — for scores and totals: model the scoring
  process (goals ~ Poisson, points ~ Normal), derive market probs analytically.
  Few parameters, strong structure — excellent for small samples.
- **Gradient boosting (XGBoost/LightGBM)** — trees capture interactions and
  non-linearities, but they overfit small samples enthusiastically and their raw
  outputs are usually MIScalibrated (recalibrate afterwards — see
  `calibrate_probabilities`). Reach for boosting when you have thousands of events
  and engineered features; never for 50 games. The sandbox has no GPU and may not
  carry xgboost — sklearn's GradientBoostingClassifier or hand-rolled logistic is
  the portable default.
- **Elo-style ratings** — not a model family, a feature factory: a single online
  rating per team, updated per game, feeds any of the above as a strong feature.

## Regularization & validation

- **Regularization** (L2/shrinkage): pull estimates toward zero/base-rate; the
  smaller the sample, the harder you shrink. Equivalent intuition: a prior.
- **Walk-forward CV**: refit at each step, predict the next slice, accumulate
  out-of-sample scores. Honest for time series where a single split is lucky/unlucky.

## Market concepts

- **CLV (closing-line value)**: your price vs the closing price. The close is the
  market's most informed state; consistently beating it is the strongest evidence
  of real edge — more reliable than short-run ROI (variance dominates small samples).
- **Vig/overround**: the book's margin baked into prices; remove it (normalise
  implied probs) before comparing model vs market — `value_finder` does this.
- **Variance vs edge**: positive CLV + negative ROI over 50 bets = variance, keep
  going; negative CLV + positive ROI = luck, stop. Bet counts under ~100 decide
  almost nothing at typical edges — say so.
