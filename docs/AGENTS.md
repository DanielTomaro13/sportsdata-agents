# Agent roster

Generated from `src/sportsdata_agents/specs/`. The **orchestrator** routes a request
to the right specialist(s), runs independent parts in parallel, and synthesises one
answer. Each agent holds only the data capabilities and tools it needs (lean by
design — every tool schema rides every call).

> This file is kept current by the **docs_keeper** ops agent (it opens a PR when
> the roster drifts). If you add or change a spec, expect a docs PR.

## Product plane (customer-facing, entitlement-gated)

| Agent | Tier | What it does |
|---|---|---|
| **orchestrator** | balanced | Routes, delegates, synthesises. Holds no data tools of its own. |
| **odds_specialist** | balanced | Cross-book prices: implied probability, fair price, best price, same-game-multi. |
| **stats_specialist** | fast | Fixtures, results, boxscores, game logs, head-to-head, season leaders, ladders, injuries. |
| **racing_analyst** | balanced | Racing: meetings, racecards, next-to-jump, results & dividends, futures, SRM, cross-book win/place. |
| **prediction_market_analyst** | balanced | Kalshi/Polymarket contracts + the exchange-vs-book edge (contract prob vs vig-removed book prob). |
| **modelling** | balanced | Builds & calibrates probability models in the sandbox; persists versions + predictions. |
| **value_scout** | fast | +EV selections: calibrated model probs vs vig-removed market (edge %, fair odds). |
| **arb_hunter** | fast | Cross-book + exchange-vs-book arbitrage; sets standing arb watches. |
| **backtester** | fast | Replays predictions vs captured odds history + results: ROI, hit-rate, CLV, variance. |
| **fantasy_advisor** | balanced | DFS/fantasy: projections, salary-cap lineup optimisation, injuries, player research. |
| **data_analysis** | balanced | Ad-hoc pandas/matplotlib analysis in a sandbox; play-by-play, advanced metrics, charts. |
| **bet_tracker** | fast | Journals your bets, settles results, reports P&L/ROI/hit-rate/CLV. |
| **bankroll_manager** | fast | Kelly/flat sizing guidance + the exposure gate (caps against bankroll + open bets). |
| **bet_notifier** | fast | Formats a recommendation for delivery (selection, book, price, sizing, reasoning, sources). |
| **news_scout** | fast | Pre-game intel from X + league news: injuries, team news, weather — confirmed vs chatter. |
| **market_steward** | fast | Maintains the canonical market dictionary (as data); safe aliases applied, ambiguous reported. |
| **concierge** | fast | Plain-language explainer of the team's findings. |
| **agent_builder** | balanced | Builds a custom agent from a plain-English goal (drafts prompt, picks data + skills, versions it). |
| **slack_manager** | fast | Slack workspace housekeeping (add-on). |

**Advisory invariant:** no product agent places a bet or moves money. Sizing tools
compute a *fraction* (`kelly_fraction`), never a stake; money-verb tool names are
denied by construction.

## Ops plane (platform maintenance — never licence-gated)

| Agent | What it does |
|---|---|
| **mcp_health** | Runs doctor + contract suite on the data plane; files issues on real breaks. |
| **incident_triage** | Watches feed health; remediates within an allow-list (retry/disable/enable) or escalates. |
| **eval_benchmark** | Runs the offline eval gate; records agent metrics; reports regressions. |
| **repo_improver** | Proposes changes from feedback/telemetry; opens CI-gated PRs a human merges. |
| **code_reviewer** | Reviews PRs (diff-driven): approve or request changes. |
| **site_manager** | Keeps the public site honest: uptime, catalogue drift, traffic, badge PRs. |
| **docs_keeper** | Keeps `docs/` in sync with the code; opens a PR when structure/roster/CLI drift. |

Ops agents only ever **open PRs / file issues** — a human merges. There is no merge
tool. This is the self-improvement loop: telemetry → proposal → CI → human.

## Data capabilities

Agents leverage ~37 of the data plane's ~60 capabilities (`agents/capability_labels.json`):
all of `racing.*`, all of `prediction.*`, the priced surfaces (`sport.prices`,
`event_markets`, `same_game_multi`), the stats surfaces (game logs, head-to-head,
play-by-play, advanced metrics, injuries, ladders, leaders), `ref.*`, `social.*` and
`content.news`. The unused remainder is redundant (e.g. `match_detail` ⊂
`match_boxscore`) or niche (broadcast, live audio/video, shot charts). A coverage
guard test (`test_capability_coverage`) keeps this from regressing.
