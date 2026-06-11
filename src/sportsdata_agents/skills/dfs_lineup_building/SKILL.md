---
name: dfs_lineup_building
description: DFS lineup workflow — site rules first, projections with stated sources, deterministic optimisation via optimize_lineup, stacking/ownership as explicit judgment.
triggers: [lineup, dfs, daily fantasy, draftkings, fanduel lineup, salary cap, optimise lineup, optimize lineup, fantasy team]
---
# DFS lineup building

Build daily-fantasy lineups: projections in, optimal lineup out — with the
judgment calls made explicit.

## Before optimising, establish (ask the user what you can't infer)
- **Site + contest rules**: roster slots, salary cap, scoring system. They differ
  (DraftKings/FanDuel/local AU sites); never assume.
- **Projection source**: the user's numbers, or build them from stats (per-game
  averages adjusted for opponent/pace/minutes). State which you used.
- **Contest type**: cash games want the highest floor (consistent scorers); GPP
  tournaments want ceiling + leverage (lower-owned players whose upside separates
  you from the field).

## Using `optimize_lineup`
- Pass EVERY candidate player with `positions`, `salary`, `projection` — the tool
  does all the math (deterministic beam search; near-optimal).
- `locked` forces players in (the user's picks or a stack); `excluded` removes
  injured/benched players. Re-run with different locks to compare builds.
- Multi-position eligibility matters: pass all listed positions; "G"/"F"/"UTIL"
  slots accept their families automatically.

## Judgment the optimiser does NOT make (you do, and say so)
- **Stacking**: correlated players (QB+WR, same-team hitters) raise ceiling for
  GPP — lock the stack, optimise the rest.
- **Late swap / news**: confirm lineups against the latest scratchings/injury
  news before presenting; flag any player whose status is uncertain.
- **Ownership leverage**: in GPP, an equal-projection pivot at lower ownership
  is usually the better tournament play.

## Honesty rules
- Projections are estimates; report the lineup's projected points as a point
  estimate, not a promise.
- Always state the slots/cap/scoring you optimised for, and the source of every
  projection. Advisory only.
