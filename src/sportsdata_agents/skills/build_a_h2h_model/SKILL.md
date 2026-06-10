---
name: build_a_h2h_model
description: Worked example of model_development for match-winner (h2h/moneyline) markets — ratings + logistic regression, home advantage, draw handling.
triggers: [h2h model, head to head model, moneyline model, winner model, match winner model]
---
# Build a head-to-head (winner) model — worked example

Follow `model_development` for the method; this is the h2h instantiation.

## Recipe (run_python, one script)

1. **Data**: results for the competition, as many seasons as the regime allows
   (count events: §2 of model_development). Print what you kept and dropped.
2. **Ratings as the core feature**: maintain an Elo-style rating per team —
   `r_new = r_old + K * (outcome - expected)`, `expected = 1/(1+10^(-(r_a-r_b)/400))`.
   Tune K on the training slice only (typical 16–32). Ratings compress a team's
   whole history into one number — ideal for small samples.
3. **Logistic regression on top**: features = rating difference, home indicator,
   rest-days difference if available, plus AT MOST one or two user-valued stats
   (ask — see model_development §3). Fit on train, freeze, predict holdout.
4. **Draws** (football, etc.): model as multinomial (home/draw/away) or fit
   P(draw) separately as a function of rating closeness — never silently ignore
   the draw in a 3-way market.
5. **Calibrate + persist**: `calibration_metrics` on the holdout →
   `save_model` (params: K, coefficients, feature list, seasons used) →
   `record_predictions` with honest `predicted_at`.

## Sanity anchors

- Home advantage exists in every league; if your fitted home coefficient is
  negative, suspect a data bug before a discovery.
- Compare against the market baseline: vig-removed closing probs. Beating Elo is
  easy; beating the close is the bar.
