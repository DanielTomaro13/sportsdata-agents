# sportsdata-agents

An agentic platform that turns the [`sportsdata-mcp`](https://github.com/DanielTomaro13/sportsdata-mcp)
tool catalogue into a **sports analytics & research platform** — a team of LLM agents that
gather and analyse sports data for **analysts, coaches, fantasy players, media and fans**,
with an **optional trading desk** (an opt-in betting module) that compares odds across
bookmakers, models outcomes, finds value, and tracks performance. A separate engineering team
of agents maintains the codebase via CI-gated PRs.

Betting is one use case, not the point: with the betting module switched off the platform is a
pure analytics tool (bigger market, lower compliance surface).

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
