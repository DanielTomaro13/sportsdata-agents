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

📐 **[`PLAN.md`](./PLAN.md)** — the full architecture: the two-plane design, the agent
roster, the user-customizable agent-spec format, the data model, orchestration & model
selection, sandboxing, interfaces (CLI → Slack → web), multi-tenancy / SaaS-readiness,
the self-improvement loop, the delivery roadmap, and a **decision register** with the
pros and cons of every choice.

## Status

Planning. The data plane ([`sportsdata-mcp`](https://github.com/DanielTomaro13/sportsdata-mcp))
is built and contract-tested; this repository is the agent plane that consumes it.

---

Private & proprietary. Copyright (c) 2026 Daniel Tomaro. All rights reserved — see
[`LICENSE`](./LICENSE).
