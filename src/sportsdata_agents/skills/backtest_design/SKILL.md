---
name: backtest_design
description: How to design and read backtests honestly — lookahead/leakage traps, point-in-time discipline, multiple comparisons, CLV vs ROI, sample size.
triggers: [backtest, backtesting, lookahead, look-ahead, replay strategy]
---
# Backtest design — the ways backtests lie

A backtest is an argument, and most of them are flawed. Check these before quoting one.

## Lookahead & point-in-time discipline

- The entry price must be one you could have GOT at prediction time. The platform's
  `run_backtest` enforces this (entry = prevailing change-point at `predicted_at`),
  so honest `predicted_at` timestamps are part of the experiment — backdating a
  prediction to grab an early price is fabricating edge.
- Features must be point-in-time too: no closing prices, no post-game stats, no
  season aggregates that include the predicted game.

## Reading the report

- **Lead with average CLV** (quant_concepts): +CLV/−ROI = variance, the strategy is
  probably fine; −CLV/+ROI = luck, it probably isn't. ROI converges over hundreds
  of bets; CLV says something useful after dozens.
- **Skips are findings**: `no_price` = warehouse coverage gap; `no_result` = settle
  the events; `below_edge` = the threshold did its job. A backtest silently built
  on 10% of predictions is a different experiment than claimed.
- **Sample size**: under ~100 bets, confidence intervals on ROI span the whole
  conclusion. Quote the bet COUNT next to every headline number; call small samples
  anecdotes.

## Multiple comparisons & survivorship

- Trying ten thresholds/feature-sets and reporting the best one is p-hacking: the
  winner's numbers are inflated by selection. Either pre-register one strategy or
  report ALL variants tried.
- Survivorship: backtesting only events that ended up with results/prices skews
  toward liquid, well-covered markets — say what fraction of the original universe
  the replay actually covered.

## Iterating

- Change ONE thing per run (threshold, feature, model version) and keep the old
  report for the diff. The eval harness pins the golden replay; your experiments
  should be similarly reproducible — persist model versions, never overwrite.
