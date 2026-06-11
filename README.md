# sportsdata-agents

An agentic platform that turns the [`sportsdata-mcp`](https://github.com/DanielTomaro13/sportsdata-mcp)
tool catalogue into **whatever desk you need** — a cross-bookmaker **trading desk**, a
**sports-analytics / coaching team**, a **fantasy desk**, or a custom mix. It's one composable
team of LLM agents over a shared data backbone: they gather and analyse sports data, model
outcomes, compare odds, optimise fantasy lineups, and track performance — **you turn on the
agents and modules that fit your purpose**. A separate engineering team of agents maintains the
codebase via CI-gated PRs.

You assemble a workspace from **modules we build and you select** — Match Analytics, Fantasy,
Racing, Trading/Betting, and more. Nothing is privileged: a Trading module and a Coaching module
are equal citizens of the catalogue. Trading/Betting is just one module (and jurisdiction-gated),
so a workspace without it is a pure analytics tool (bigger market, lower compliance surface).

> ### Advisory only — no agent ever places a bet or moves money.
> The platform **informs**. It surfaces recommendations, the statistics you asked for,
> and the bets *you* may choose to place (with stakes, books, and reasoning). **You
> always take the action.** This is a research and analytics tool, not a betting operator.

## Design

🛠️ **[`BUILD_PLAN.md`](./BUILD_PLAN.md)** — the technical, phase-by-phase implementation
checklist to tick off while coding (P0 → P4, milestones, exit gates).

📐 **[`PLAN.md`](./PLAN.md)** — the full architecture: the two-plane design, the agent
roster, the user-customizable agent-spec format, the data model, orchestration & model
selection, sandboxing, interfaces (CLI → Slack → web), multi-tenancy / SaaS-readiness,
the self-improvement loop, the delivery roadmap, and a **decision register** with the
pros and cons of every choice.

## What's built (P0–P2 complete)

**The agent team** (14 specs in `src/sportsdata_agents/specs/`): an orchestrator that
routes and delegates; odds/stats specialists over the live data plane; a modelling agent
(general model development — features, calibration, Brier/log-loss, logistic regression,
XGBoost skills); a value scout (vig removal, +EV detection, cross-book best price); a
backtester (entry-at-prediction-time discipline, CLV vs close); bankroll manager, bet
tracker and bet notifier (advisory only); a market steward that maintains the market
dictionary as data; Slack manager; data-analysis agent (sandboxed `run_python`); concierge.
Every answer is **grounded** (numbers must come from tool results — a deterministic
verifier checks), **sourced** (provenance per tool call), **budgeted** (one cost ceiling
per team run) and **audited** (runs/tool-calls/costs land in the DB when configured).

**The odds warehouse** (`agents ingest`): discovery-driven, capture-everything ingestion
across **10 bookmakers** — Sportsbet, TAB, Unibet/Kambi, Entain (Ladbrokes/Neds),
Pinnacle, PointsBet, BetR, FanDuel (US sportsbook + racing) — in four tiers:

- **hot** (5–30 min): every provider's own discovery route → all sports, all
  competitions, primary + inline markets — nothing hardcoded, new comps/sports appear
  automatically;
- **full-book** (60 min): every market of every fixture (rotating windows over the
  megabyte-scale per-fixture firehoses);
- **racing** (~3 min): win+place cards from TAB, Sportsbet, BetR, PointsBet, Unibet,
  FanDuel, soonest races first;
- **racing futures** (60 min): ante-post Cup/carnival outrights from TAB, Sportsbet,
  PointsBet, Unibet.

Sports futures (premiership winners, Brownlow, NFL/MLB futures, …) ride the hot tier:
Kambi `competitions.json`, Sportsbet's Outrights route, Entain/BetR/TAB inline.
Normalizers never filter by market name — `canonical_market()` only *renames* onto shared
keys (h2h/spread/total/win/place), driven by a **market dictionary that is data**
(packaged seed + steward-maintained local overrides; merge safety enforced in the tools:
qualifier markets can never alias into base families). Raw observations land in
`odds_snapshots` (prunable; carries the parsed advertised start time); the
change-point-only `prices` series is what models and backtests read.

**Event resolution** (`agents resolve`): deterministic, LLM-free mapping of every book's
private event ids onto shared fixtures (fuzzy-subset team-token matching, swap-tolerant,
windowed on the event's advertised start so futures join months ahead; ambiguity is
counted and skipped, never guessed). That join powers `cross_book_prices` (best price per
selection across every mapped book), resolution-aware backtest settlement (a result
recorded under any book settles every book's series, with side-orientation translated
between books' listing orders), and the `find_fixture` / `best_prices` agent tools.

**The quant loop**: `save_model` refuses uncalibrated models → `record_predictions`
(backdatable `predicted_at`; side-relative selections must name their book) →
`run_backtest` (flat-stake replay, no lookahead: entry is the prevailing price at
prediction time; CLV vs close, or vs a sharp benchmark via `clv_book="Pinnacle"`) →
`agents results` settles from racing placings + official NBA/AFL/NRL scoreboards →
`agents eval` gates golden metrics (calibration, CLV backtest, grounding, event
resolution) against a committed baseline.

## Quickstart

This is a **private repository**; you need read access to both repos.

```bash
# 1) The data plane (sibling checkout, its own venv)
git clone git@github.com:DanielTomaro13/sportsdata-mcp.git
cd sportsdata-mcp && python -m venv .venv && .venv/bin/pip install -e . && cd ..

# 2) The agent plane
git clone git@github.com:DanielTomaro13/sportsdata-agents.git
cd sportsdata-agents && python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# 3) Configure (.env — see .env.example)
#    - ONE model key: ANTHROPIC_API_KEY / OPENROUTER_API_KEY / GEMINI_API_KEY /
#      GROQ_API_KEY / OPENAI_API_KEY  (Gemini + Groq have free tiers)
#    - point at the data plane binary:
#      SPORTSDATA_AGENTS_MCP_COMMAND=["/abs/path/to/sportsdata-mcp/.venv/bin/sportsdata-mcp"]

# 4) Database (audit rows + the odds warehouse). SQLite works out of the box:
#      SPORTSDATA_AGENTS_DATABASE_URL="sqlite+aiosqlite:////path/to/warehouse.db"
#    or Postgres via docker compose up -d; then:
.venv/bin/alembic upgrade head

# 5) Talk to the team
.venv/bin/agents run "Using MLB data: which team does Aaron Judge play for?"
.venv/bin/agents chat                       # interactive REPL (/exit to quit)
.venv/bin/agents run "..." --agent value_scout        # one agent instead of the team
.venv/bin/agents list && .venv/bin/agents lint        # spec catalogue + validation
```

### The data loop

```bash
.venv/bin/agents ingest --once              # one capture cycle across all due feeds
.venv/bin/agents ingest --once --feed tab_racing_futures   # a single feed
.venv/bin/agents ingest --loop              # scheduled loop (per-feed cadence)
.venv/bin/agents ingest --once --prune 90   # retention for raw snapshots
.venv/bin/agents resolve                    # map book events -> shared fixtures
.venv/bin/agents resolve --dry-run          # count without writing
.venv/bin/agents results                    # settle: racing placings + NBA/AFL/NRL scoreboards (cron daily)
.venv/bin/agents steward                    # market_steward dictionary audit (cron weekly)
.venv/bin/agents dictionary-promote --write # promote steward overrides into the committed seed
.venv/bin/agents movement --event <id>      # change-point series for one event
.venv/bin/agents eval --baseline src/sportsdata_agents/evals/baseline.json
.venv/bin/agents refresh-books              # weekly: re-verify bookmaker ids
```

The ingest worker runs the MCP as a scoped subprocess per provider group and tolerates
per-feed failures (one book down never sinks the cycle). Known exclusions, by policy or
upstream fault: NBA CDN (aggregator — books of record only), Betfair (public key returns
no prices from AU; code ready for an authed key), Entain racing GraphQL (upstream
persisted-query drift; Entain *sports* REST — including its outrights — is unaffected).

## Testing

```bash
.venv/bin/pytest                  # offline suite (default: not live, not eval) — CI runs this
.venv/bin/pytest -m live          # real MCP + real model (needs a key; ~cents or free tier)
.venv/bin/pytest -m eval          # golden eval cases, graded for factual accuracy
.venv/bin/ruff check . && .venv/bin/mypy src   # the other two gates
```

## Status

**P2 complete** — the full quant loop over a discovery-driven, capture-everything odds
warehouse (10 books × 40+ sports × 1,200+ distinct markets, futures included), event
resolution with cross-book pricing and resolution-aware settlement, the market-steward
dictionary loop, and the eval gate. Next (P3): scheduled ingestion deployment,
Postgres/Timescale migration, market monitor agent. See
[`BUILD_PLAN.md`](./BUILD_PLAN.md) for the milestone log.

---

Private & proprietary. Copyright (c) 2026 Daniel Tomaro. All rights reserved — see
[`LICENSE`](./LICENSE).
