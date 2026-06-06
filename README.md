# sportsdata-agents

An agentic platform that turns the [`sportsdata-mcp`](https://github.com/DanielTomaro13/sportsdata-mcp)
tool catalogue into a collaborative **sports research & trading desk** — a team of LLM
agents that gather data, compare odds across bookmakers, model outcomes, find value,
track performance, supply fantasy-sports insight, and maintain their own codebase.

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
