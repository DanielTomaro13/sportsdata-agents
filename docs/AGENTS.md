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
| **live_desk** | balanced | **In-play**: live scores, in-play prices, momentum, and a fair cash-out valuation beside the book's own quote. |
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
| **generalist** | balanced | The **catch-all**: handles requests no specialist covers, computes in a sandbox, and **grows the platform** — writes reusable skills and builds new agents as it learns your needs. |
| **agent_builder** | balanced | Builds a custom agent from a plain-English goal (drafts prompt, picks data + skills, versions it). |
| **slack_manager** | fast | Slack workspace housekeeping (add-on). |

**Advisory invariant:** no product agent places a bet or moves money. Sizing tools
compute a *fraction* (`kelly_fraction`), never a stake; money-verb tool names are
denied by construction.

### The growth loop (the generalist)

The platform learns. When the orchestrator gets a request no specialist fits, it
routes to the **generalist**, which solves it with a sandbox + the data plane and
then — only for genuinely reusable patterns — **crystallises** the result:

- **`create_skill`** writes a prose playbook (SKILL.md) into the user's own skill
  library (`<data_dir>/skills/`). Next time, `list_skills` + `recall_skill` pull it
  back. Skills are markdown, never code — they guide the LLM and can't grant a tool
  or bypass the advisory/no-money rules.
- **`save_agent_spec`** (the agent_builder path) promotes a recurring need into a
  dedicated, versioned agent that persists locally — run it with `agents run --agent <id>`.

`agents skills` lists what's been learned. This is local-first: the platform grows on
the user's machine, to the user's needs, and nothing leaves it. (Pro tier — capability
creation is a power feature.)

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

Agents grant **37 of the data plane's 68** capability tags: all of `racing.*`, all of
`prediction.*`, the priced surfaces (`sport.prices`, `event_markets`, `same_game_multi`),
the stats surfaces (game logs, head-to-head, play-by-play, advanced metrics, injuries,
ladders, leaders), `ref.*`, `social.*` and `content.news`.

The other 31 are **not** all redundant or niche, which is what this file used to claim.
They include the entire live plane — `sport.in_play` (20 providers) and
`sport.match_score` (36) are the two best-supported capabilities nobody grants — plus all
four `fantasy.*` tags, cross-sport discovery, and player depth. Counting both doors
(capability tags and `mcp_groups`), agents reach **453 of 826 tools**; the rest sit behind
a capability nobody granted, or carry no tag at all.

Every unwired capability now carries a written reason in
[`capability-waivers.yaml`](./capability-waivers.yaml) — `planned` with a phase,
`redundant`, `niche`, or `upstream`. Run the ledger any time:

```
python3 scripts/capability-audit.py            # the full picture
python3 scripts/capability-audit.py --check    # what CI runs
```

### Carried vs reached

A spec grants capabilities two ways. `mcp_capabilities` are **carried** — their tool
schemas ride every model call. `mcp_discover` are **reached**: found at turn time with
`find_data_tools` and invoked with `call_data_tool`, costing two schemas however far the
capabilities fan out. Same reach, different price.

Carry what the agent uses on nearly every question; discover the rest. Two rules:

- **Wide lookups belong in discover.** `stats_specialist` moved its reference layer
  (fixtures, players, teams, ladder — 207 tools) and dropped from 261 carried tools to
  66, about 12,500 tokens to 3,200.
- **Single-provider capabilities must never be carried.** `bridge_mcp_tools` treats a
  capability resolving to zero tools as a spec error, so a carried one stops the agent
  starting whenever that provider is toggled off. Discovery just returns nothing.
  Guarded by `test_a_single_provider_capability_is_never_carried`.

`capability_labels.json` is **generated**, not hand-edited (`--regenerate`). It was
hand-maintained and fell five entries behind the data plane, which is what blinded the
old coverage guard: it asserted a floor of 30 *intersected against that stale copy*, so
it could not see the tags it was missing. The guard now asserts that every published
capability is wired or waived, and that the labels still match upstream.
