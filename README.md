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

# 4) (optional) Postgres for audit rows — without it the CLI still works, just unaudited
docker compose up -d && .venv/bin/alembic upgrade head

# 5) Talk to the team
.venv/bin/agents run "Using MLB data: which team does Aaron Judge play for?"
.venv/bin/agents chat                       # interactive REPL (/exit to quit)
.venv/bin/agents run "..." --agent stats_specialist   # one agent instead of the team
.venv/bin/agents list && .venv/bin/agents lint        # spec catalogue + validation
```

Every answer is **grounded** (numbers must come from tool results — a deterministic
verifier checks), **sourced** (provenance envelope per tool call), **budgeted** (one
cost ceiling per team run), and **audited** (runs/tool-calls/costs land in Postgres
when configured).

## Testing

```bash
.venv/bin/pytest                  # offline suite (default: not live, not eval) — CI runs this
.venv/bin/pytest -m live          # real MCP + real model (needs a key; ~cents or free tier)
.venv/bin/pytest -m eval          # golden eval cases, graded for factual accuracy
```

## Status

**P0 complete** — orchestrator + odds/stats specialists over the live data plane
([`sportsdata-mcp`](https://github.com/DanielTomaro13/sportsdata-mcp), 18 providers /
342 tools), with the full harness (loop control, context policy, skills, typed
outputs, grounding verification), cost metering + audit persistence, and the CLI.
Next (P1): Slack interface, bet tracking + CLV, first sandbox. See
[`BUILD_PLAN.md`](./BUILD_PLAN.md) for the milestone log.

---

Private & proprietary. Copyright (c) 2026 Daniel Tomaro. All rights reserved — see
[`LICENSE`](./LICENSE).
