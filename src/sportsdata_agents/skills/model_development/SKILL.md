---
name: model_development
description: The general model-building method for ANY market — framing, sample-size discipline, feature selection (user priors + data-driven), baselines, leakage-safe validation, calibration, persistence.
triggers: [build a model, train a model, model development, predictive model, new model, build me a model]
---
# Model development — the general method

This is the method for every market (winners, totals, lines, props). Market recipes
(`build_a_totals_model`, `build_a_h2h_model`) are worked examples OF this method,
never replacements for it.

## 1. Frame the problem

- What exactly is being predicted? One binary probability per selection is the
  default (P(home win), P(over)). Name the market convention (whose line, which
  book) before any code.
- What is the decision the probability feeds? A backtest entry needs the prob
  BEFORE the price moves — timestamps are part of the model contract
  (`record_predictions` takes `predicted_at`; never backdate dishonestly).

## 2. Sample size BEFORE cleverness

- Rule of thumb: **10–20 outcomes per model parameter** as a floor. A logistic
  regression with 5 features wants 100+ decided events; anything fancier wants
  several times that.
- "Last 20 games" is rarely enough — it estimates a mean with ±20%+ noise and one
  parameter eats half of it. Prefer **multiple seasons with recency weighting**
  (exponential decay) over tiny recent windows.
- Sport cadence changes everything: an MLB team plays 162 games/season, an NFL team
  17 — the same "two seasons of data" is 324 events in one sport and 34 in the
  other. Count EVENTS, not calendar time.
- Watch regime changes: rule changes, roster turnover, venue moves. Old data is
  only valuable while the process that generated it still operates — say so when
  you truncate history and why.
- When the data cannot support the model requested, SAY THAT PLAINLY and build the
  smaller model that it can support.

## 3. Features: ask AND measure

- When the request is open-ended, **ask the user which stats they believe matter**
  for this market — domain priors are real information and it is their model.
- Independently **measure importance from the data** (univariate signal, simple
  permutation importance in run_python) — then report where the user's priors and
  the data disagree, with numbers. Do not silently drop either.
- Fewer features beat more: every feature is a parameter and §2 already priced
  parameters. Justify each one in a sentence.

## 4. Baseline first

- The market's vig-removed probabilities ARE the baseline (`value_finder` shows
  them). A model that cannot beat the closing line has no edge regardless of its
  Brier — report your model AND the market baseline side by side.
- The second baseline is the base rate (home teams win X%). Beat both or say so.

## 5. Validate without lying to yourself

- Time-ordered splits only: train on the past, test on the future. **Never shuffle
  time**; walk-forward (rolling refit) is the gold standard when you have volume.
- Leakage checklist before quoting any holdout number: no post-game stats in
  pre-game features, no closing prices as features for an entry-time model, no
  target-derived columns, holdout events strictly after every training event.
- Report holdout Brier/log-loss via `calibration_metrics` (see `quant_concepts`
  for how to read them), then `save_model` — it refuses uncalibrated models by
  design. Record forward picks with `record_predictions`.

## 6. Honesty rules

- Print every number in run_python; quote only printed numbers.
- Probabilities only — sizing belongs to the bankroll manager; no locks, no
  guarantees, the user decides.
