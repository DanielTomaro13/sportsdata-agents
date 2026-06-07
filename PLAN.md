# sportsdata-agents — Architecture & Delivery Plan

| | |
|---|---|
| **Status** | Draft for review (v0.1) |
| **Owner** | Daniel Tomaro |
| **Last updated** | 2026-06-06 |
| **Data plane** | [`sportsdata-mcp`](https://github.com/DanielTomaro13/sportsdata-mcp) (built, contract-tested) |
| **This repo** | `sportsdata-agents` — the agent plane |

---

## 0. TL;DR

We are building a **team of cooperating LLM agents** on top of the `sportsdata-mcp` tool
catalogue. The same composable team becomes **whatever the user configures it to be** — a
cross-bookmaker **trading desk**, a **sports-analytics / coaching team**, a **fantasy desk**, or
a custom mix — serving analysts, coaches, fantasy players, media and fans as readily as bettors.
It gathers and analyses sports data, models outcomes, compares odds, optimises lineups, and
tracks performance; **each workspace turns on the agents and modules it wants** (betting is one
opt-in module). A separate engineering team of agents maintains the codebase by opening pull
requests that must pass CI and review.

Three rules shape every decision in this document:

1. **Advisory only.** No agent places a bet or moves money — ever. The system reports,
   recommends, and notifies; the **user** acts. This is enforced architecturally (no agent
   is granted a betting/withdrawal credential or tool), not just by prompt.
2. **Two planes, cleanly separated.** The *data plane* (`sportsdata-mcp`) stays a pure,
   reusable, contract-tested tool boundary. The *agent plane* (this repo) owns
   orchestration, reasoning, state, and product surfaces.
3. **Local-first, SaaS-ready.** We run single-user/local now, but every design choice
   (multi-tenant data model, per-workspace secrets, stateless gateway, scoped tools) is
   made so that turning this into a multi-tenant SaaS is configuration and hardening — not
   a rewrite. Crucially, the agents themselves are split into a customer-facing **product
   plane** and an operator-only **operations plane** ([§3.1](#31-two-agent-planes--product-vs-operations-the-saas-split)),
   so customers can never reach your codebase, your platform credentials, or another tenant's
   data. Where a decision has a meaningful trade-off, it is recorded in
   [§19 Decision register](#19-decision-register) with pros and cons.

---

## Table of contents

1. [Vision & scope](#1-vision--scope)
2. [Guiding principles](#2-guiding-principles)
3. [System architecture](#3-system-architecture)
   - [3.1 Two agent planes — product vs operations (the SaaS split)](#31-two-agent-planes--product-vs-operations-the-saas-split)
4. [Repository & code organization](#4-repository--code-organization)
5. [Technology stack](#5-technology-stack)
6. [The agent team](#6-the-agent-team)
7. [Agent specification format](#7-agent-specification-format)
8. [Orchestration & model selection](#8-orchestration--model-selection)
9. [Data & state model](#9-data--state-model)
10. [Sandboxing & code execution](#10-sandboxing--code-execution)
11. [Interfaces](#11-interfaces)
12. [Multi-tenancy & SaaS-readiness](#12-multi-tenancy--saas-readiness)
    - [12.1 Pricing, packaging & entitlements (SaaS)](#121-pricing-packaging--entitlements-saas)
13. [Security, secrets & guardrails](#13-security-secrets--guardrails)
14. [Compliance & responsible use](#14-compliance--responsible-use)
15. [The self-improvement loop](#15-the-self-improvement-loop)
16. [Observability, cost tracking & evaluation](#16-observability-cost-tracking--evaluation)
17. [Deployment topology](#17-deployment-topology)
18. [Delivery roadmap](#18-delivery-roadmap)
19. [Decision register](#19-decision-register)
20. [Risks & mitigations](#20-risks--mitigations)
21. [Glossary](#21-glossary)
- [Appendix A — Example agent specs](#appendix-a--example-agent-specs)
- [Appendix B — Example end-to-end flows](#appendix-b--example-end-to-end-flows)

---

## 1. Vision & scope

### What it is
A conversational, multi-agent **sports desk that is whatever you configure it to be** — a
cross-bookmaker **trading desk**, a **sports-analytics / coaching team**, a **fantasy desk**, or
a custom blend. It is *one composable team of agents over the same data backbone*; each workspace
turns on the agents and modules that fit its purpose. A user (you today; a client tomorrow) asks
a question or sets up a standing job, and the team answers using live and historical sports data
(and, when the betting module is enabled, bookmaker prices):

**Analytics & research (no gambling involved):**
- *"Show me our next opponent's last-five defensive splits and where they concede."* — coach / analyst
- *"Trend player X's workload and shooting efficiency across the season."* — performance analyst
- *"Optimise my DFS lineup for Saturday."* / *"Who should I start this week?"* — fantasy
- *"Summarise last night's match with the key stats and storylines."* — media / fan

**Trading desk (opt-in betting module):**
- *"Where's the best price on the Pies tonight, and is there value?"*
- *"Build a model for AFL totals and alert me when the line disagrees."*
- *"How did my tracked bets do last month — ROI and closing-line value?"*

**Platform self-maintenance (operator):**
- *"Are any data feeds broken?"* / *"Add a new data provider."*

### Who it's for (personas)
The statistics surface is valuable far beyond betting. From one data backbone the platform
serves several audiences — and **betting is just one of them**:

| Persona | Wants | Primary agents |
|---|---|---|
| **Coaches & performance analysts** | Opponent scouting, player workload/efficiency trends, matchup splits | Stats specialist · Data-analysis · Modelling |
| **Fantasy / DFS players** | Projections, lineup optimisation, player research | Fantasy advisor · Stats specialist |
| **Media & content** | Fast, accurate match summaries, storylines, records | Stats specialist · Concierge |
| **Fans** | "How did my team do", standings, player comparisons | Stats specialist · Concierge |
| **Bettors / traders** *(opt-in module)* | Best price, value, CLV, bankroll, alerts | Odds · Value · Bankroll · Bet-notifier · Line-monitor |
| **Operators (you)** | Healthy feeds, an improving codebase, cost control | Operations plane (§3.1) |

**You decide what it is — composable presets.** A workspace turns on the agents and modules it
wants. Nothing is privileged: a trading desk and a coaching desk are equal configurations of the
same team. Example presets:
- **Trading desk** — odds + value + bankroll + line-monitor + tracking (betting module on).
- **Analytics / coaching desk** — stats + data-analysis + modelling + opponent scouting (betting off).
- **Fantasy desk** — fantasy advisor + stats + lineup optimisation (betting off).
- **Custom** — any mix, or a bespoke agent the user defines (§7, via the agent-builder).

The betting agents are a **module** enabled/disabled **per tenant and per jurisdiction**; with it
off, the same platform is a pure analytics tool — a bigger addressable market and materially lower
compliance exposure (§14).

### What it explicitly is **not** (non-goals)
- **It never places bets or moves money.** No agent is wired to a stake-placement,
  deposit, or withdrawal endpoint. The strongest action an agent can take toward a bet is
  to **notify** the user of a recommended bet (selection, stake, book, reasoning) for the
  user to place manually.
- It is not a tipping service or a guarantee of profit; it is a decision-support tool.
- It is not (initially) a general chatbot — it is scoped to sports data, odds, modelling,
  fantasy, performance tracking, and self-maintenance.

### Primary user outcomes
1. **Stats & analytics** — fixtures, boxscores, player/team trends, opponent scouting, summaries.
2. **Modelling** — predictive models, backtests, calibrated probabilities.
3. **Fantasy / DFS** — projections, lineup optimisation, player research.
4. **Odds intelligence** *(betting module)* — best price, fair price, value, arbs, line movement.
5. **Performance tracking** *(betting module)* — log bets the user places, settle, report P&L / ROI / CLV.
6. **Self-maintenance** — keep the data feeds healthy and the codebase improving.

---

## 2. Guiding principles

| # | Principle | Why it matters |
|---|---|---|
| P1 | **Separation of planes** | Data access (MCP) is reusable infra; agents are application logic. Independent testing, release, and reuse. |
| P2 | **Spec-driven agents** | Agents are declared in YAML (like providers in `sportsdata-mcp`), so users customise/add agents with no code. |
| P3 | **Least privilege** | Each agent gets only the tool groups, secrets, and sub-agents it needs — scoped via the MCP's existing group system. |
| P4 | **Human-in-control for money** | Advisory only; placement is always a human action. Enforced by *capability*, not prompt. |
| P5 | **Model-agnostic** | Any LLM, chosen per task by the orchestrator. No lock-in. |
| P6 | **Multi-tenant from day one (logically)** | `tenant_id`/`workspace_id` threads through data and secrets even while there's one tenant. SaaS becomes config, not rewrite. |
| P7 | **Everything observable & evaluable** | Every agent run, tool call, model choice, and recommendation is traced and scoreable. Feedback drives self-improvement. |
| P8 | **Determinism at the edges** | Data fetching, schema validation, staking math, and settlement are deterministic code; the LLM reasons, it doesn't do arithmetic that matters. |
| P9 | **Reproducibility** | Snapshots of odds/data are persisted so any recommendation can be explained and any backtest re-run. |

---

## 3. System architecture

Two planes. The agent plane consumes the data plane over the MCP protocol; it never
reaches the upstream sports/bookmaker APIs directly.

```
┌──────────────────────────────────────────── INTERFACES ─────────────────────────────────────────┐
│        CLI            Slack            Discord            Web app (SaaS)        REST/Webhook        │
└───────────┬───────────────┬───────────────┬──────────────────┬──────────────────────┬─────────────┘
            └───────────────┴───────────────┴──────────────────┴──────────────────────┘
                                              │  (channel-agnostic messages)
                                              ▼
┌──────────────────────────────── AGENT GATEWAY (FastAPI, async) ─────────────────────────────────┐
│  AuthN/Z · tenant resolution · rate/cost limits · task queue · streaming · audit                  │
│                                              │                                                     │
│                                   ┌──────────▼──────────┐                                          │
│                                   │     ORCHESTRATOR    │   intent → plan → delegate → synthesise │
│                                   │  (router + model    │   + guardrails (no-money invariant)     │
│                                   │   selection)        │                                          │
│                                   └──────────┬──────────┘                                          │
│        ┌───────────────┬────────────────┬────┴───────────┬─────────────────┬────────────────┐     │
│   Domain specialists  Quant agents   Tracking/alerts   Fantasy advisor   Engineering dept   Product│
│        │                  │                │                 │                  │              │     │
└────────┼──────────────────┼────────────────┼─────────────────┼──────────────────┼──────────────────┘
         │ MCP tools         │ sandbox+history │ DB              │ MCP+models       │ git/GitHub+CI
         ▼                   ▼                 ▼                 ▼                  ▼
┌──────────────────┐  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐  ┌──────────────────────┐
│  sportsdata-mcp  │  │  Sandboxes   │  │  Postgres /  │  │  Model gateway │  │ sportsdata-mcp +     │
│  (data plane,    │  │ (E2B/Modal/  │  │  Timescale + │  │ (LiteLLM/      │  │ sportsdata-agents    │
│  group-scoped)   │  │  Docker)     │  │  object store│  │  OpenRouter)   │  │ repos (PRs, CI)      │
└──────────────────┘  └──────────────┘  └──────────────┘  └───────────────┘  └──────────────────────┘
```

### Core components
- **Agent gateway** — a stateless FastAPI service: receives channel-agnostic messages,
  resolves tenant/user, enforces auth + rate/cost limits, kicks off agent runs (sync for
  fast asks, async task for long jobs), streams results back, and writes the audit log.
- **Orchestrator** — the conductor agent (see [§8](#8-orchestration--model-selection)).
- **Agent runtime** — Pydantic AI agents instantiated from YAML specs, each with a scoped
  toolset (a filtered view of `sportsdata-mcp` + native Python tools + optional sub-agents).
- **Data plane** — one or more `sportsdata-mcp` instances, started with
  `SPORTSDATA_MCP_GROUPS` scoped per agent (least privilege, P3).
- **State** — Postgres (+ Timescale for odds time-series), object store for artifacts.
- **Sandboxes** — ephemeral, isolated environments for code execution and internet access
  (modelling, analysis, the engineering agents).
- **Model gateway** — unified access to all LLM providers with per-tenant keys/budgets.
- **Observability** — tracing + evaluation over every run.

### 3.1 Two agent planes — product vs operations (the SaaS split)

For SaaS, the agents fall into **two classes with completely different trust boundaries,
credentials, triggers, billing, and blast radius.** Designing the split now — even while you
are the only user — is what makes the SaaS transition safe rather than a re-architecture. (It
is a *security and privacy boundary*, not merely an org chart.)

**Product plane — tenant agents (what a customer uses).**
- Run *inside a tenant workspace*, scoped to that tenant's data, secrets, config, and budget.
- Invoked by customers through the customer interfaces (Slack / Discord / web).
- Hold only *per-workspace* secrets (the tenant's own keys) — never platform credentials.
- Usage is metered and billed to the tenant.
- Members: orchestrator, domain specialists, quants, reporting/tracking, fantasy, concierge,
  agent-builder (Tiers 0–4 and 6 in §6).

**Operations plane — platform / operator agents (what *you* use to run and improve the platform).**
- Run under the *platform's own identity*, not any tenant's; they are cross-tenant or
  repo/infra-facing.
- **Never reachable from the customer gateway.** Triggered only by the operator (an admin
  console / internal CLI), schedules, or CI/webhook events.
- Hold *platform* credentials — GitHub repo-write, CI, infra — that **no tenant agent can see**.
- Cost is platform opex, not billed to customers.
- Members: MCP health/QA, repo-improver, code-reviewer, eval/benchmark, plus future
  platform-ops agents — incident triage, cost watchdog, provider-unblock (Tier 5 in §6).

```
 customers ──► Customer gateway ──► PRODUCT PLANE ─────► per-workspace data + tenant secrets
               (auth, tenancy,      (orchestrator + specialists + quants + fantasy + concierge)
                budgets)                   │
                                           │  emits ONLY aggregated, anonymized signals
                                           ▼
 operator ───► Operator console ──► OPERATIONS PLANE ──► platform creds (GitHub / CI / infra)
 (admin/RBAC)  + schedules + CI     (QA · improver · reviewer · eval)  ──► CI-gated PRs,
                                                                           human-merged
```

**Why the hard split**
- **Blast radius / security** — a prompt-injection or jailbreak in a tenant agent must not be
  able to reach repo-write credentials or another tenant's data. Separate identities + no
  inbound path from customer traffic enforce this in infrastructure, not prompts.
- **Privacy** — improvement must never leak one customer's data into the codebase or to another
  tenant. The operations plane consumes only **aggregated, anonymized** signals from the product
  plane (eval scores, feed health, opt-in performance metrics) — never raw tenant data.
- **Billing & SLA** — customer usage and your platform opex are different ledgers.
- **Lifecycle** — you ship platform changes on your cadence; customers consume the product
  continuously.

**How the planes connect — one-way and sanitized.** Product plane → emits aggregated/anonymized
eval, performance, and health signals → operations plane prioritizes fixes and improvements →
changes land as **CI-gated, human-merged PRs** → a new platform version rolls out to all tenants.
Every repo write and the merge gate live entirely in the operations plane (§15).

**Today (single-user / local):** both planes run side-by-side under one principal (you). The
seams — separate credential sets, separate trigger paths, and a separate package + deployable
for operations — go in now, so SaaS is *turning on isolation + an operator console*, not
re-architecting. Whether operations becomes its own repo/service is [D14](#19-decision-register).

---

## 4. Repository & code organization

**Decision: separate repos** (see [§19, D1](#19-decision-register)). `sportsdata-mcp`
stays standalone and is consumed as a dependency.

```
sportsdata-agents/
├── README.md
├── PLAN.md                     ← this document
├── pyproject.toml              ← package: "sportsdata_agents"
├── src/sportsdata_agents/
│   ├── gateway/                ← PRODUCT entry: customer-facing FastAPI app, auth, tenancy, queue, streaming
│   ├── operations/             ← OPERATIONS entry: operator console + engineering agents
│   │                             (separate deployable; platform creds; off the customer gateway) — §3.1, D14
│   ├── orchestrator/           ← router, planner, model-selection policy (product plane)
│   ├── agents/                 ← SHARED agent runtime + loader (reads agent specs; used by both planes)
│   ├── specs/                  ← *.yaml agent definitions (user-customizable)
│   │   ├── _schema.yaml        ← the agent-spec contract (mirrors pydantic models)
│   │   ├── orchestrator.yaml
│   │   ├── odds_specialist.yaml
│   │   └── ...
│   ├── mcp/                    ← MCP client manager (scoped sessions per agent)
│   ├── tools/                  ← native (non-MCP) tools: staking math, DB, charts
│   ├── sandboxes/              ← sandbox provider abstraction (E2B/Modal/Docker)
│   ├── data/                   ← DB models, migrations, repositories
│   ├── models/                 ← LLM gateway config + model-policy
│   ├── interfaces/             ← cli/, slack/, discord/, web/  (thin adapters)
│   ├── eval/                   ← evaluation harness + metrics
│   └── observability/          ← tracing wiring
├── tests/
│   ├── unit/ · integration/ · contract/   ← mirrors the mcp repo's test discipline
└── .github/workflows/          ← lint, tests, agent-spec validation, eval gates
```

**Dependency flow:** `interfaces → gateway → orchestrator → agents → (mcp client | tools |
sandboxes | data | models)`. One-directional; no cycles.

**Plane split ([§3.1](#31-two-agent-planes--product-vs-operations-the-saas-split), [D14](#19-decision-register)):**
the shared runtime — `agents/`, `specs/`, `mcp/`, `tools/`, `data/`, `models/`, `sandboxes/` —
lives once. The **product** plane (`gateway/` + the tenant-facing agents) and the **operations**
plane (`operations/` + the engineering agents) are **separate deployables with separate
credentials and trigger paths**, in the same repo for now. Only the operations deployable is
given the platform secrets (GitHub/CI/infra); only the product deployable is exposed to
customers.

---

## 5. Technology stack

Each row notes the choice and the key trade-off; the deeper pros/cons live in
[§19](#19-decision-register).

| Layer | Choice | Why (short) |
|---|---|---|
| **Agent framework** | **Pydantic AI** | Model-agnostic, **native MCP client**, typed outputs, agent delegation, Logfire integration. |
| **Model gateway** | **LiteLLM** (self-host) or **OpenRouter** (hosted) | One interface to all LLMs; per-tenant keys + budgets; the orchestrator swaps models freely. |
| **Gateway/API** | **FastAPI** + async workers | Async-native, streaming, typed, ubiquitous. |
| **Task queue** | **Arq** or **Celery** (Redis) | Long jobs (modelling, PRs) run off the request path with status updates. |
| **Database** | **Postgres** + **TimescaleDB** | Relational state + first-class time-series for odds history/CLV. |
| **Object store** | S3-compatible (MinIO local / S3 prod) | Model artifacts, charts, large payloads. |
| **Sandboxes** | **E2B** or **Modal** (managed) → Docker/Firecracker (self-host) | Isolated code+internet; managed first for speed, portable later. |
| **Observability** | **Pydantic Logfire** (or Langfuse) | Traces of every agent/tool/model call; eval dashboards. |
| **Interfaces** | CLI (Typer) → Slack (Bolt) → Discord (discord.py) → Web (Next.js) | Adapters over one core; see [§11](#11-interfaces). |
| **Auth (SaaS)** | Clerk / Auth0 / Supabase Auth | Deferred until SaaS; gateway has auth seams now. |
| **Lang/runtime** | Python 3.12+ | Matches `sportsdata-mcp`; the data/ML ecosystem. |

> **Why not LangGraph / CrewAI / AutoGen?** They're capable, but Pydantic AI's typed
> outputs + native MCP support + lighter footprint fit a data-heavy, tool-centric system
> better, and keep us aligned with the MCP repo's pydantic-first style. If the
> orchestrator's control flow becomes a complex stateful graph, `pydantic-graph` (same
> family) covers it without switching frameworks. Full comparison in [§19, D2](#19-decision-register).

---

## 6. The agent team

Agents are grouped into tiers. Each is defined by a YAML spec ([§7](#7-agent-specification-format)).
"Tools" lists the **scoped** MCP groups / capabilities and native tools it may use.
"Gating" notes any human checkpoint. **No agent has bet-placement or money tools.**

> **Plane ([§3.1](#31-two-agent-planes--product-vs-operations-the-saas-split)):** Tiers 0–4 and
> 6 are the **Product plane** (tenant-facing, customer-invokable, per-workspace scope). **Tier 5
> is the Operations plane** — platform/operator-only, **never customer-invokable**, holds the
> platform credentials, triggered by the operator / schedules / CI.
>
> **Betting module ([§1](#1-vision--scope)):** the odds & sharp-reference specialists (Tier 1),
> value-finder & bankroll/risk (Tier 2), and the reporting/alerts agents (Tier 3) make up the
> **opt-in betting module**. The stats specialist, data-analysis, backtesting, modelling,
> fantasy, and concierge agents serve coaches / analysts / fantasy / media **with the betting
> module switched off** — same platform, no gambling features.

### Tier 0 — Control plane
| Agent | Purpose | Tools | Model tier |
|---|---|---|---|
| **Orchestrator** | Classify intent, plan, delegate (parallel where possible), pick the model per task, synthesise, enforce guardrails | sub-agents; no data tools directly | fast → escalates |
| **Memory service**¹ | User prefs, session history, long-term facts, retrieval | DB / vector store | n/a |

¹ A shared service, not a conversational agent; all agents read/write it.

### Tier 1 — Domain specialists (read the world via MCP)
These ride the MCP **capability tags**, so they're cross-provider by default — much
stronger than one-agent-per-book.

| Agent | Purpose | Tools (scoped) |
|---|---|---|
| **Odds specialist** | Best price, fair price (vig-removed), value, arbs, across all books | `sport.prices`, `sport.event_markets`, `sport.competition_screen` |
| **Racing specialist** | Meetings, cards, results, scratchings, next-to-jump, SRMs, futures | `racing.*` across TAB/Sportsbet/BetR/PointsBet/Entain/RacingAndSports |
| **Stats specialist** | Fixtures, boxscores, player/team stats, standings, play-by-play, telemetry | MLB/AFL/NBA/NRL/cricket/OpenF1/ESPN/DataGolf data groups |
| **Live/in-play specialist** | Live scores, in-play markets, win-probability, momentum | `sport.in_play`, `sport.match_score`, win-prob feeds |
| **Sharp-reference specialist** | Pinnacle/Betfair as "true price" benchmark for value detection | `pinnacle.*`, `betfair.exchange` |

### Tier 2 — Quant agents (turn data into edges)
| Agent | Purpose | Tools | Notes |
|---|---|---|---|
| **Modelling agent** | Build/run predictive models; output **calibrated** probabilities | sandbox + history store + stats specialist | strong model |
| **Value / edge-finder** | Model prob vs market price → +EV bets, edge %, fair odds | odds + modeller outputs; staking math (native) | deterministic math |
| **Data-analysis agent** | Ad-hoc analysis, charts, feature engineering | sandbox + DB | the "data scientist" |
| **Backtesting agent** | Replay historical odds+results → ROI/CLV/variance | odds-history warehouse | depends on persisted snapshots |
| **Bankroll / risk manager** | Staking strategy (Kelly/flat), exposure & correlation limits, portfolio view | DB + native staking math | **gate before any recommendation is surfaced** |

### Tier 3 — Reporting, tracking & alerts (no placement)
| Agent | Purpose | Tools | Notes |
|---|---|---|---|
| **Bet-notification agent** | Surfaces recommended bets to the user — *selection, suggested stake, which book, reasoning* — for the user to place manually | reads odds/value/risk outputs | **notifies only; never places** |
| **Bet-tracking / P&L agent** | Records bets the user tells it they placed (or logs manually), settles outcomes from results feeds, computes P&L / ROI / **CLV**, hit-rate by market/sport | DB + results feeds | source of truth for performance |
| **Line-monitor / alerting agent** | Standing watch for line moves, steam, scratchings, value appearing/vanishing → pushes alerts | odds/live + scheduler | long-running; pushes to channel |

### Tier 4 — Fantasy / DFS
| Agent | Purpose | Tools |
|---|---|---|
| **Fantasy advisor** | Projections, lineup optimisation, player research, slate analysis (e.g. DFS, season-long) | DataGolf fantasy, MLB/NBA/AFL stats, optimisation in sandbox |

### Tier 5 — Engineering department · **Operations plane** (maintains the repos)
*Platform/operator-only — runs under the platform identity with platform credentials, never
exposed to customers (§3.1). Consumes only aggregated/anonymized signals from the product plane.*
| Agent | Purpose | Tools | Gating |
|---|---|---|---|
| **MCP health / QA agent** | Run `doctor` + the contract suite on a schedule; detect breakage/shape drift; file issues | sandbox + the mcp repo's CLI/tests | alerts humans |
| **Repo-improver / scout** | Propose changes from feedback (new providers, endpoints, model/prompt tweaks); **author PRs** | sandbox + git + GitHub API | PR only — never self-merges |
| **Code-reviewer agent** | Review PRs (improver's and humans'): correctness, security, contract-tests-pass | sandbox (read) + GitHub API | approve/request changes; **human merges** |
| **Eval / benchmark agent** | Score quality over time (calibration, routing, betting performance) → feedback for the improver | DB + eval harness | closes the loop |

### Tier 6 — Product / interaction
| Agent | Purpose | Tools |
|---|---|---|
| **Concierge / explainer** | Turn quant output into plain language; answer "why this bet"; own per-channel UX | reads all agent outputs |
| **Agent-builder** | Help a user describe an agent in natural language and write its YAML spec | spec schema + validator |

> **Granularity note.** The default is **capability/domain** specialists (cross-provider),
> not 18 per-book agents — the whole point of the MCP capability-tag system. Per-provider
> agents (e.g. a Betfair Exchange specialist) are added only where a book has real quirks
> worth isolating, and any user can spin one up via a YAML spec.

---

## 7. Agent specification format

The lever for "users create their own agents, fully customizable." An agent is a YAML
file — declarative, validated, version-controlled, and diff-reviewable. This mirrors how
`sportsdata-mcp` defines providers, so the mental model carries over.

```yaml
# src/sportsdata_agents/specs/odds_specialist.yaml
spec_version: 1

agent:
  id: odds_specialist
  display_name: "Odds Specialist"
  description: "Compares prices across all books; computes fair price and value."

  # Model is a *tier*, resolved to a concrete model by the model-policy (§8) so users
  # don't hard-code vendor models and the orchestrator can override per task.
  model_tier: balanced            # fast | balanced | strong  (or an explicit "anthropic:claude-..." )

  system_prompt: |
    You are an odds specialist. Use the provided tools to fetch live prices across books,
    remove the vig to estimate fair probability, and report best price + value. Never
    place bets; only report. Show your sources and timestamps.

  # Least-privilege tool scope. Either MCP capability tags (preferred — cross-provider) or
  # explicit groups. The runtime starts/【scopes the MCP session to exactly these.
  tools:
    mcp_capabilities: [sport.prices, sport.event_markets, sport.competition_screen]
    mcp_groups: []                # optional explicit groups
    native: [vig_removal, implied_probability]   # deterministic helpers
  forbidden_capabilities: []       # hard deny-list (defense in depth)

  # Delegation: which other agents this one may call as tools.
  can_delegate_to: [stats_specialist]

  # Execution policy
  sandbox: none                    # none | ephemeral  (code execution / internet)
  secrets: []                      # named secret refs this agent may read (per-workspace)
  output_type: OddsComparison      # a registered pydantic result schema (typed output)

  limits:
    max_tool_calls: 25
    max_tokens: 120000
    timeout_seconds: 120
    cost_ceiling_usd: 0.50         # per run; enforced by the gateway
```

Key properties:
- **`model_tier`** keeps specs vendor-neutral; the [model policy](#8-orchestration--model-selection)
  maps tiers → concrete models per tenant/budget.
- **`tools.mcp_capabilities`** uses the cross-provider tags, so "compare odds" needs no
  per-book wiring.
- **`forbidden_capabilities`** + the runtime's hard exclusion of any money/placement tool
  enforce the advisory-only invariant in code.
- **`output_type`** gives typed, validated results (great for chaining and for the UI).
- **`limits`** are enforced by the gateway (cost/latency guardrails), critical for SaaS.

---

## 8. Orchestration & model selection

**Orchestrator responsibilities:** intent classification → task decomposition → delegation
(parallel fan-out where independent) → result synthesis → guardrail enforcement → cost/latency
budgeting. Implemented as a Pydantic AI agent whose "tools" are the other agents
(delegation); complex stateful routing can graduate to `pydantic-graph`.

**Model-selection policy** (config, not code) — a map resolved at run time:

```yaml
# models/policy.yaml
tiers:
  fast:     { default: "anthropic:claude-haiku", fallback: "openai:gpt-5-mini" }
  balanced: { default: "anthropic:claude-sonnet", fallback: "google:gemini-pro" }
  strong:   { default: "anthropic:claude-opus",  fallback: "openai:gpt-5" }
routing:
  intent_classification: fast
  data_lookup:           fast
  odds_comparison:       balanced
  modelling:             strong
  code / PR authoring:   strong
overrides_by_tenant: {}            # SaaS: per-tenant model allow-lists + budgets
```

- The orchestrator picks a **tier per task**; the policy resolves the tier to a concrete
  model via the gateway, with automatic fallback on error/rate-limit.
- **Pros of tier-based routing:** vendor-neutral specs, central cost control, easy A/B of
  models, per-tenant budgets for SaaS. **Cons:** an indirection layer to maintain, and a
  mis-tiered task wastes money or under-reasons — mitigated by the eval agent measuring
  routing quality.

---

## 9. Data & state model

The MCPs are **stateless fetchers**; the platform must persist what they can't. Every
table carries `tenant_id` and (where relevant) `workspace_id` from day one (P6) so SaaS is
a flip, not a migration.

Core entities (Postgres; `odds_snapshots` and `prices` on TimescaleDB hypertables):

| Table | Purpose |
|---|---|
| `tenants`, `workspaces`, `users`, `memberships` | Identity & isolation (one tenant today). |
| `agent_specs` | Registered agent definitions (DB-backed for user-created agents; files for built-ins). |
| `conversations`, `messages` | Per-channel chat history + context. |
| `agent_runs`, `tool_calls` | **Audit**: every run, model used, tokens, cost, latency; every tool call + args + result hash. |
| `usage_ledger` | **Cost spine**: normalised per-run cost — model tokens (in/out) × price, sandbox seconds (+GPU), tool calls, latency, outcome — tagged with tenant/workspace/agent/task-type/model-tier. Basis for cost dashboards, per-agent ROI, and SaaS billing (§16.1). |
| `budgets` | Per-workspace / per-agent spend caps + running balances, enforced by the gateway. |
| `agent_metrics` | Rolled-up efficiency per agent: cost-per-successful-task, success rate, value-add, quality, latency (§16.2). |
| `subscriptions`, `entitlements` | Tenant plan + included quotas + enabled add-ons + Stripe ids — the source of truth the gateway checks before enabling an MCP / agent / interface or starting a run (§12.1). |
| `fixtures`, `events`, `selections` | Normalised entities resolved from feeds (cross-provider keys). |
| `odds_snapshots`, `prices` | **Time-series** of prices per selection/book — the basis for line movement, CLV, backtests. |
| `models`, `predictions` | Trained models + their probability outputs (with calibration metadata). |
| `tracked_bets` | Bets the **user** placed (logged manually or via confirmation), with stake/odds/book; settled from results. |
| `recommendations` | What we *suggested* (selection, stake, book, edge, reasoning) — distinct from `tracked_bets`. |
| `performance` | Derived P&L / ROI / CLV / hit-rate, by market/sport/strategy. |
| `evals`, `feedback` | Quality scores + feedback that drives the improver. |
| `alerts`, `subscriptions` | Standing watches and their delivery targets. |
| `secrets_refs` | Names + pointers (not values) to per-workspace secrets in the secret store. |

Two deliberate separations:
- **`recommendations` vs `tracked_bets`** — the system recommends; the user decides. Keeping
  them distinct is what lets us measure recommendation quality *and* the user's realised
  results, and it reinforces the advisory-only boundary.
- **Snapshots are immutable** — every recommendation references the exact `odds_snapshot`
  it was made from, so it's explainable and reproducible (P9).

---

## 10. Sandboxing & code execution

Several agents must execute code and reach the internet (modelling, analysis, backtests,
the engineering department). This is the highest-risk surface, so it's isolated.

**Abstraction:** a `Sandbox` interface (`run(code, files, network_policy) → result`) with
swappable backends, so the choice below is reversible.

| Option | Pros | Cons |
|---|---|---|
| **E2B** (managed) | Purpose-built for AI code execution; fast cold start; SDK; per-run isolation | Usage cost; external dependency; data leaves your box |
| **Modal** (managed) | Great for heavy/parallel compute + GPUs (modelling); generous; Pythonic | Cost; more infra concepts; external |
| **Daytona / Fly Machines** | Cheap, flexible microVMs you control | More ops; you manage isolation & lifecycle |
| **Self-host Docker** | Full control, cheapest, data stays local | Weaker isolation than microVMs; you own security hardening |
| **Self-host Firecracker** | Strong isolation (microVM); good for multi-tenant | Significant ops complexity |

**Recommendation:** start with **E2B (or Modal for compute-heavy modelling)** for speed,
behind the `Sandbox` abstraction; revisit **self-hosted Firecracker** *if and when* SaaS
multi-tenancy or data-residency requires it. (See [§19, D5](#19-decision-register).)
Network egress from sandboxes is allow-listed; secrets are injected per-run and never
persisted in the image.

---

## 11. Interfaces

All interfaces are **thin adapters** over the channel-agnostic gateway. The agent core
never knows the channel; adding one is an adapter, not a rewrite.

| Interface | Effort | Effectiveness | When |
|---|---|---|---|
| **CLI** (Typer) | Lowest | High for dev/iteration | **First** — prove the agent loop fast |
| **Slack** (Bolt) | Low–med | High — threads map to a *team* of agents; mobile + push alerts; credible client demo | **Second** |
| **Discord** (discord.py) | Low–med | High if audience is community/retail | Optional, parallel to Slack |
| **Web app** (Next.js) | High | Necessary for SaaS (auth, billing, P&L dashboards, odds visualisation) | **Only when going SaaS** |
| **REST/Webhook** | Low | Enables integrations (e.g. push alerts elsewhere) | As needed |

**Recommendation: CLI → Slack → (Web only for SaaS).** Rationale: least work first; Slack's
threaded, multi-participant model is a natural fit for a team of agents and gives push
notifications (the line-monitor's alerts) and a client-ready demo for almost no extra work;
the heavy web build is deferred until a paying product justifies it. **Slack vs Discord:**
Slack reads as a "trading desk"/enterprise tool (better SaaS story); Discord wins only if
the eventual users are retail communities. (See [§19, D4](#19-decision-register).)

---

## 12. Multi-tenancy & SaaS-readiness

We run single-tenant/local now but **bake the seams in** so SaaS is hardening + config:

- **Tenancy in the data model (P6):** `tenant_id`/`workspace_id` on every row; all queries
  filter by it (enforced centrally in the repository layer + Postgres Row-Level Security
  when SaaS turns on). One tenant today, N tomorrow.
- **Per-workspace secrets:** secret *references* in the DB, values in a secret store
  (env/file now → Vault/cloud KMS in SaaS). An agent only reads secrets scoped to its
  workspace and spec.
- **Stateless gateway:** no per-request server state → horizontal scaling is trivial later.
- **Auth seams:** the gateway has an auth middleware boundary that's a no-op locally and
  swaps to Clerk/Auth0/Supabase for SaaS — interfaces already pass a principal.
- **Cost & rate limits per tenant:** the `limits` in agent specs + a gateway budget ledger
  meter usage per workspace (essential for billing/abuse control).
- **Config over code:** a workspace **preset** (trading / analytics / fantasy / custom — §1),
  the set of enabled agents and modules, model allow-lists, enabled MCP groups, and budgets are
  all per-workspace config. "What this desk is" is data, not a code branch.
- **The operations plane is platform-level, not per-tenant ([§3.1](#31-two-agent-planes--product-vs-operations-the-saas-split)):**
  operator/engineering agents run under the platform identity with platform credentials, are
  never exposed on the customer gateway, and consume only aggregated/anonymized cross-tenant
  signals — so customer data never crosses into the codebase or another workspace.

**Pros of baking it in now:** SaaS later is config + hardening, not a rewrite; cleaner
boundaries even for single-user; multi-"workspace" is useful even solo (e.g. separate
bankrolls/strategies). **Cons:** a little extra plumbing (a `tenant_id` you "don't need"
yet) and discipline to always scope queries. Net: low cost now, very high option value —
**recommended**. (See [§19, D6](#19-decision-register).)

### 12.1 Pricing, packaging & entitlements (SaaS)

The same composable platform is monetised by **entitlements over its building blocks** — data
sources (MCP providers), agents, interface, seats, and usage. The per-workspace config that
already defines "what this desk is" (§12) *is* the entitlement set; billing simply grants or
limits it. (Prices below are placeholders — the actual numbers are a go-to-market decision, not
an architectural one, and should be market-validated.)

**Billable units**

| Unit | What it is | How it's billed |
|---|---|---|
| **MCP providers** | each enabled data source (AFL, MLB, a bookmaker, …) | N included per tier; extra = add-on ($X each / mo) |
| **Agents** | enabled product-plane agents (preset or custom) | N "of choice" per tier; extra = add-on |
| **Interface** | API + CLI → chat (Slack/Discord) → web app | unlocked by tier |
| **Seats** | users in a workspace | N included, then per-seat |
| **Usage allowance** | LLM tokens + sandbox compute (the §16.1 `usage_ledger`) | monthly allowance per tier; overage metered or via top-up packs |
| **Betting module** | the opt-in betting agents | add-on; jurisdiction-gated entitlement (§14) |

**Tiers** (mirrors your sketch — each tier is a superset of the one below)

| | **Tier 1 — Starter** | **Tier 2 — Pro** | **Tier 3 — Team** |
|---|---|---|---|
| **Interface** | API + CLI | **+ Slack / Discord** | **+ Web app** (dashboards, P&L & odds viz) |
| **MCP providers included** | **3** (extra: $X each) | 3 (extra: $X each) | more (extra: $X each) |
| **Agents** | presets only | **3 of choice** + agent-builder | 3+ of choice (more available) |
| **Seats** | 1 | a few | **multi-seat** (per-seat add-on) |
| **Usage allowance** | base | higher | highest |
| **Betting module** | add-on | add-on | add-on |
| **Indicative price** | `$ / mo` | `$$ / mo` | `$$$ / mo` |

So: **Tier 1** = pay-per-MCP with 3 included (API/CLI); **Tier 2** = Tier 1 + a non-web (chat)
interface + 3 agents of choice; **Tier 3** = Tier 2 + the web app — extras (MCPs, agents, seats)
metered on top at every tier.

**Add-ons & metered extras (any tier):** additional MCP provider · additional agent · additional
seat · betting module · premium-model access · extra usage packs.

**The variable-cost reality (why pure-flat won't work).** Every run costs us real money — LLM
tokens + sandbox compute, metered in `usage_ledger` (§16.1). A flat, unlimited subscription loses
money on heavy users and invites abuse, so the *cost-recovery* model matters as much as the tier
ladder:

| Model | Pros | Cons |
|---|---|---|
| Flat / all-you-can-use | Simplest; predictable bill for the customer | Margin risk; abusable; one heavy user erodes profit |
| Pure usage-based | Margins protected; "pay for what you use" | Unpredictable bills deter customers; harder to forecast |
| **Hybrid — allowance + metered overage (recommended)** | Predictable *and* margin-safe; reuses the meter + budgets already built | Allowances must be communicated clearly; metering UX to get right |

**Recommendation:** each tier **includes a usage allowance**; overage is metered (or bought as
top-up packs); per-workspace **budgets/ceilings (§16.1) hard-cap** spend so the customer is never
surprised and we are never out of pocket.

**Enforcement & infra.** Entitlements are per-workspace config (§12); the gateway checks them
**before** enabling an MCP group, instantiating an agent, exposing an interface, or starting a run
that would breach budget. **Stripe** (subscriptions + metered usage) is the billing system; the
`usage_ledger` feeds the metered components; upgrades/downgrades are config changes, not
migrations. An optional limited **free tier / trial** (e.g. 1 MCP, presets only, capped usage) is
a low-cost acquisition lever. Decisions: **D18** (packaging), **D19** (cost recovery), **D20**
(seats) in §19.

---

## 13. Security, secrets & guardrails

- **Plane isolation ([§3.1](#31-two-agent-planes--product-vs-operations-the-saas-split)).**
  Product (tenant) agents and operations (operator) agents have *separate identities and
  credential sets*. Tenant agents never hold platform credentials (GitHub/CI/infra); operator
  agents are unreachable from the customer gateway. This caps the blast radius of any
  tenant-side compromise and keeps customer data out of the codebase.
- **The no-money invariant is structural.** The MCP tool catalogue exposed to agents is
  filtered to exclude any placement/deposit/withdrawal tool; agent specs cannot grant one;
  the runtime denies them even if requested. Advisory-only is enforced by capability, not
  just by prompt.
- **Secrets** are never in specs or prompts; they're per-workspace references resolved at
  run time and injected only into the agent/sandbox that needs them.
- **Prompt-injection defense:** treat all fetched web/feed content as untrusted; tools
  return structured data, not instructions; the orchestrator strips/ignores instruction-like
  content from tool results; sandboxes have allow-listed egress.
- **Audit everything money-adjacent:** every recommendation, every tracked bet, every tool
  call is logged immutably with inputs/outputs and the model used.
- **Engineering agents** operate only via PRs on a branch; they cannot push to `main` or
  merge; CI (lint + contract + offline tests) is a hard gate; a human merges.
- **Rate/cost ceilings** per agent run and per tenant prevent runaway spend.

---

## 14. Compliance & responsible use

Advisory-only positioning materially lowers (but doesn't erase) regulatory exposure: we
provide research/analytics, not a betting service, and never handle stakes or funds.

- **Composable; betting is opt-in (§1).** A workspace configured without the betting module is a
  pure sports-analytics tool — sellable to coaches, clubs, fantasy players and media with **no
  gambling-regulation surface at all**. Enable the betting module only per tenant and only where
  the jurisdiction permits; entitlements gate it centrally.
- **Single-user/local:** personal research tool — low risk.
- **SaaS:** offering betting-adjacent tooling to others can engage gambling-advertising,
  consumer-protection, data-protection (PII/GDPR), and jurisdiction rules. **Get legal
  advice before selling.** Build in: clear "not financial/betting advice" disclaimers,
  responsible-gambling messaging and self-exclusion/limit hooks, age/jurisdiction gating,
  and per-tenant data isolation/retention. These are cheap to stub now and expensive to
  retrofit — so the seams go in early, the policies turn on at SaaS.

---

## 15. The self-improvement loop

The engineering department closes the loop the user described — improving the system based
on measured performance and testing:

```
        ┌──────────────────────────────────────────────────────────────────────┐
        │                                                                        │
   performance &      Eval/benchmark agent           Repo-improver           Code-reviewer
   feedback   ─────►  scores calibration, routing, ─► proposes change ─────► reviews PR,
   (P&L, CLV,         CLV, data-feed health          (new provider,          runs/reads CI
   QA results)                                        model/prompt tweak)     (lint+contract+
        ▲                                              │  opens PR             offline tests)
        │                                              ▼                          │
        │                                       sportsdata-mcp / -agents          │
        │                                       branch + PR                       ▼
        └──────────────────────  human reviews & MERGES  ◄──────────  approve / request changes
                                  (the only merge gate)
```

- **Inputs:** betting performance (`performance` table), eval scores, and the MCP **QA
  agent's** contract/`doctor` results.
- **Guardrails:** improver opens PRs only; the **contract test suite we built in
  `sportsdata-mcp`** is the objective gate (a feed shape change or broken spec fails CI);
  the reviewer agent comments; **a human merges.** No autonomous changes to `main`.
- **Plane boundary ([§3.1](#31-two-agent-planes--product-vs-operations-the-saas-split)):** the
  loop spans both planes, but the product plane only *emits* aggregated, anonymized signals —
  all repo writes, the reviewer, and the human merge gate live entirely in the **operations
  plane**. Customers cannot trigger, see, or influence it beyond opt-in aggregate metrics.

---

## 16. Observability, cost tracking & evaluation

- **Tracing:** every agent run, delegation, tool call, model choice, token count, latency,
  and cost is traced (Logfire/Langfuse). Essential for debugging multi-agent flows and for
  per-tenant cost accounting.

### 16.1 Cost tracking

Spend is never a mystery — every run carries a full cost attribution.

- **What's metered:** LLM tokens (in/out) × per-model price + sandbox seconds (and GPU) + tool
  calls + wall-clock latency, written to `usage_ledger` (§9) and tagged with **tenant,
  workspace, agent, task type, model tier, and conversation**.
- **Roll-ups:** cost per run / agent / task type / tenant / day on the dashboard — *"where is
  the money going?"* is one query.
- **Budgets & ceilings:** per-run `cost_ceiling_usd` (agent spec, §7) + per-workspace `budgets`
  (§9), enforced by the gateway. A run that would breach is throttled, **down-shifted a model
  tier**, or paused for approval. In SaaS this same ledger is the **billing meter**.
- **Cost-watchdog (operations plane):** alerts on anomalies (a spec that suddenly costs 5×, a
  runaway loop) and proposes cheaper tiers where eval shows no quality loss.

### 16.2 Agent efficiency — "are we getting what we need from each agent?"

Each agent is measured as an **investment**, not just traced. The eval/benchmark agent
(operations plane) computes these on a schedule and on PRs and writes them to `agent_metrics`
(§9), so an underperforming or over-priced agent is *visible* and actionable.

| Metric | The question it answers |
|---|---|
| **Cost per successful task** | What does a *useful* answer from this agent actually cost? |
| **Success / completion rate** | How often does it produce a valid, used result (vs error, empty, discarded)? |
| **Value-add** | Did its output get used downstream / accepted by the user / improve the final answer? |
| **Quality** | Calibration (Brier/log-loss) for models; **CLV** for betting recs; rubric / LLM-judge scores for narrative answers |
| **Latency** | Time-to-answer per agent and task type |
| **Routing efficiency** | Is the model tier right — over-paying on easy tasks, or under-reasoning hard ones? |
| **Tokens / tool-calls per task** | Is the agent efficient, or thrashing? |

The payoff: **"ROI of each agent" is a first-class, dashboarded number.** A weak or expensive
agent can be retuned (prompt / model tier), have its scope narrowed, or be retired — and the
self-improvement loop (§15) can propose those changes automatically.

### 16.3 Evaluation

The eval agent runs scheduled and PR-triggered evals — model calibration (Brier/log-loss vs
outcomes), recommendation quality (CLV — did we beat the closing line?), routing efficiency
(cost vs quality), answer quality for analytics/narrative tasks, and data-feed health. Eval
results gate *"is this change actually better?"* before the improver's PRs are taken seriously.

---

## 17. Deployment topology

| | **A. Local / single-user (now)** | **B. SaaS / multi-tenant (later)** |
|---|---|---|
| **Compute** | One VM or your machine | PaaS (Fly/Railway/Render) or cloud (ECS/GKE), autoscaled |
| **DB** | Local Postgres + Timescale | Managed Postgres + Timescale, backups, RLS on |
| **Sandboxes** | One E2B/Modal account (or local Docker) | Per-tenant isolation; managed sandboxes or Firecracker |
| **Secrets** | `.env` / local file | Vault / cloud KMS, per-workspace |
| **Auth/billing** | None | Clerk/Auth0 + Stripe metering |
| **Effort** | Low | High (tenancy, billing, support, SLAs, compliance) |
| **Cost** | LLM + a small box | Infra + support + compliance |

**Recommendation:** ship **Mode A** now on the SaaS-ready architecture (§12). Flip to
**Mode B** only after the agents prove value and the legal question (§14) is answered.

---

## 18. Delivery roadmap

Each phase is shippable and de-risks the next. Maps to the agent roster in §6.

| Phase | Goal | Agents / components | Exit criteria |
|---|---|---|---|
| **P0 — Foundations** | One real flow end-to-end on a CLI | Gateway skeleton, MCP client manager, **Orchestrator + Odds + Stats specialists**, Postgres, agent-spec loader, tracing | "Best price + value on tonight's game" works from CLI with audit + traces |
| **P1 — Track & converse** | Slack + performance | Slack adapter, **Bet-notification**, **Bet-tracking/P&L (CLV)**, **Bankroll/risk**, **Concierge**, first **sandbox** for **Data-analysis** | Log a user's bets, report ROI/CLV in Slack; one analysis runs in a sandbox |
| **P2 — Quant** | Models that beat the line | **Modelling**, **Value-finder**, **Backtesting**, odds-history warehouse | A model backtests with CLV > 0 on held-out data; value alerts fire |
| **P3 — Self-maintaining** | The engineering dept + alerts + fantasy | **MCP health/QA**, **Improver**, **Reviewer**, **Eval**, **Line-monitor**, **Fantasy advisor**, **Agent-builder**, Discord | QA agent opens an issue on a broken feed; improver lands a CI-passing PR; a user builds a custom agent from chat |
| **P4 — Productize (optional)** | SaaS | Auth, **billing + tiers/entitlements (§12.1)**, web app, per-tenant isolation hardening, compliance policies | A second tenant onboarded on a paid tier with isolated data, enforced entitlements + budgets, and disclaimers |

---

## 19. Decision register

The decisions that need a call, each with options, a recommendation, and the trade-off.
Status: **(set)** = chosen with you; **(open)** = needs your steer.

| ID | Decision | Options | Recommendation | Pros / Cons of the recommendation |
|---|---|---|---|---|
| **D1** | Repo strategy | Separate repos / monorepo / extend mcp | **Separate** *(set)* | + Clean planes, independent CI, MCP stays pure & reusable. − Two repos to coordinate; cross-cuts need two PRs. |
| **D2** | Agent framework | **Pydantic AI** / LangGraph / CrewAI / AutoGen | **Pydantic AI** *(set)* | + Native MCP, typed outputs, light, pydantic-aligned, Logfire. − Younger ecosystem than LangChain; complex graphs need pydantic-graph. |
| **D3** | Model gateway | LiteLLM (self-host) / OpenRouter (hosted) / direct SDKs | **LiteLLM** to start | + One API, per-tenant keys+budgets, self-host = data control. − A component to run; OpenRouter is simpler but adds a hop + margin. |
| **D4** | First interface(s) | CLI→Slack / Slack-first / Discord-first / Web-first | **CLI → Slack** *(set)* | + Least work first, fastest iteration; Slack = team feel + alerts + demo. − Slack needs a workspace/app setup; Discord better only for retail. |
| **D5** | Sandbox provider | E2B / Modal / Daytona / Docker / Firecracker | **E2B (Modal for heavy compute)**, behind an abstraction | + Fast, isolated, low ops. − Cost + external dependency; revisit self-host for SaaS/residency. |
| **D6** | Multi-tenancy timing | Bake in now / add later | **Bake in now (logical)** *(set)* | + SaaS becomes config not rewrite; useful "workspaces" even solo. − Small upfront plumbing + query-scoping discipline. |
| **D7** | Database | Postgres+Timescale / Postgres-only / +ClickHouse | **Postgres + Timescale** | + One system, great time-series for odds/CLV. − Timescale adds an extension to operate; ClickHouse only if odds volume explodes. |
| **D8** | Observability | Logfire / Langfuse / OpenTelemetry-only | **Logfire** | + Native Pydantic AI integration, low setup. − Hosted (or self-host); Langfuse if you want OSS-first eval UI. |
| **D9** | Agent definition | YAML spec / Python code / DB-only | **YAML spec (+ DB for user-created)** *(set)* | + User-customizable, reviewable, versioned, matches mcp. − A schema to maintain; very dynamic logic may still need code tools. |
| **D10** | Hosting (when not local) | Fly / Railway / Render / own cloud | **Fly or Railway** to start | + Fast, cheap, container-native, portable. − Less control than raw cloud; revisit for scale/compliance. |
| **D11** | Memory/RAG | None early / pgvector / dedicated vector DB | **pgvector (in Postgres)** when needed | + No new system, good enough early. − Not as fast as a dedicated store at large scale. |
| **D12** | LLM providers in the pool | *(set)* | **Anthropic + OpenAI/Google + a cheap fast model**, swappable via the gateway | + Quality, redundancy, and cost control; no lock-in. − Several vendor keys/accounts to manage. |
| **D13** | SaaS go/no-go & legal | *(set: build SaaS-ready now; launch gated on legal)* | Architecture is SaaS-ready (§12, §12.1); **legal review before any paid launch** | + No rework to productize. − A gambling-adjacent sale needs legal sign-off (§14) before go-live. |
| **D14** | Operations-plane packaging ([§3.1](#31-two-agent-planes--product-vs-operations-the-saas-split)) | Same repo (separate package + deployable) / separate repo `sportsdata-ops` / same runtime as product | **Same repo, separate package + separate deployable now; split to its own repo/service when SaaS hardening demands it** | + Shares the agent runtime & spec format (one place to evolve the framework) while deploying with its own credentials and trigger path. − A softer boundary than two repos; needs discipline that operator code/creds never bundle into the tenant runtime. |
| **D15** | How operations consumes tenant signals | Raw / aggregated+anonymized / opt-in granular | **Aggregated + anonymized by default; opt-in for finer detail** | + Privacy/compliance and customer trust by construction; safe for self-improvement. − Coarser debugging of a single tenant — mitigated by time-boxed, tenant-authorized support sessions. |
| **D16** | Betting: core vs module ([§1](#1-vision--scope)) | Always-on betting / **toggleable, off-by-default module (composable presets)** / two separate products | **Toggleable module, off by default** | + One composable platform serves coaches/analysts/fantasy/media *and* bettors; bigger market; far lower compliance surface where it's off (§14). − An entitlements/feature-flag layer to maintain; shared agents must honour the toggle. |
| **D17** | Cost-attribution granularity ([§16.1](#161-cost-tracking)) | Per-run only / per-run + per-agent + per-tenant rollups / full per-tool-call | **Per-run + per-agent + per-tenant rollups (per-tool-call detail kept in the audit log)** | + Enough to bill, budget, and judge each agent's ROI without excess storage. − Slightly more write volume than per-run-only; mitigated by Timescale rollups + retention. |
| **D18** | Packaging model ([§12.1](#121-pricing-packaging--entitlements-saas)) | Tiered per-feature entitlements (MCP/agents/interface/seats) + add-ons / flat all-in tiers / pure usage | **Tiered entitlements + add-ons (per your sketch), each tier including a usage allowance** | + Price tracks both value and our cost; clear upsell path; fits the composable model. − More billing logic + quota UX to maintain. |
| **D19** | Cost recovery: LLM + sandbox ([§12.1](#121-pricing-packaging--entitlements-saas)) | Flat / pure usage / hybrid allowance + overage | **Hybrid: per-tier allowance + metered overage + hard budgets ([§16.1](#161-cost-tracking))** | + Predictable for the customer *and* margin-safe; reuses the `usage_ledger` meter. − Allowances must be communicated; metering adds complexity. |
| **D20** | Seats / collaboration ([§12.1](#121-pricing-packaging--entitlements-saas)) | Single-seat / per-seat from the web tier | **Per-seat from Tier 3 (web / teams)** | + Monetises team use where collaboration actually happens. − Seat management + invite/RBAC UX overhead. |

---

## 20. Risks & mitigations

| Risk | Mitigation |
|---|---|
| An agent is coaxed toward placing a bet | No money/placement tool exists in the catalogue; structural deny, not prompt-only (§13) |
| Bookmaker APIs geo-block / change shape | The MCP **contract tests** + QA agent detect drift; alerts + PRs (§15) |
| Runaway LLM/sandbox cost | Per-run + per-tenant budgets, tier routing, eval on routing efficiency |
| Bad/over-confident model recommendations | Calibration evals, CLV as the truth metric, human always decides, clear disclaimers |
| Prompt injection via feed/web content | Untrusted-by-default handling, structured tool returns, sandbox egress allow-list |
| Legal exposure if sold | Advisory-only positioning + compliance seams; legal review before SaaS (§14) |
| Multi-agent flows hard to debug | Full tracing of every run/tool/model (§16) |
| A customer reaches an operator agent, or one tenant's data leaks into the codebase / another tenant | Hard plane split (§3.1, §13): separate identities + credentials, operator agents off the customer gateway, and only aggregated/anonymized signals cross from product → operations |
| Vendor lock-in (LLM/sandbox) | Gateway + sandbox abstractions keep both swappable |

---

## 21. Glossary

- **Data plane / agent plane** — the MCP tool layer vs the orchestration/reasoning layer.
- **Product plane / operations plane** — *within* the agent plane: the customer-facing tenant
  agents vs the operator-only platform/engineering agents. Different identities, credentials,
  triggers, and blast radius (§3.1). Don't confuse this axis with data-vs-agent plane above.
- **Capability tag** — a provider-agnostic slug (e.g. `sport.prices`) that makes tools from
  different providers interchangeable; the basis for cross-bookmaker agents.
- **CLV (closing-line value)** — whether a bet's price beat the market's closing price; the
  gold-standard measure of betting-recommendation quality.
- **Vig / overround** — the bookmaker's margin baked into prices; removing it estimates the
  "fair" probability.
- **HITL** — human-in-the-loop; here, the user is always the one who places any bet.
- **Workspace / tenant** — an isolated unit of data, secrets, config, and budget.

---

## Appendix A — Example agent specs

**Bet-notification agent (advisory; never places):**
```yaml
spec_version: 1
agent:
  id: bet_notifier
  display_name: "Bet Notifier"
  description: "Surfaces recommended bets for the user to place manually. Never places bets."
  model_tier: balanced
  system_prompt: |
    Present recommended bets clearly: selection, suggested stake (from the risk manager),
    which book has the best price, the edge, and the reasoning. Make explicit that the USER
    places the bet. Never imply you can place it. Always include a responsible-gambling note.
  tools:
    mcp_capabilities: [sport.prices]
    native: [format_bet_ticket]
  forbidden_capabilities: ["*placement*", "*deposit*", "*withdraw*"]   # defense in depth
  can_delegate_to: [odds_specialist, value_finder, bankroll_manager]
  sandbox: none
  output_type: BetRecommendationList
  limits: { max_tool_calls: 15, cost_ceiling_usd: 0.30, timeout_seconds: 90 }
```

**MCP health/QA agent (engineering dept):**
```yaml
spec_version: 1
agent:
  id: mcp_health
  display_name: "MCP Health / QA"
  description: "Runs doctor + the contract suite; reports feed breakage and shape drift."
  model_tier: fast
  system_prompt: |
    Run the sportsdata-mcp doctor and contract tests in a sandbox. Summarise failures
    (which provider/endpoint, status), distinguish transient (skip) from real breaks, and
    open a GitHub issue for genuine breaks with a minimal repro.
  tools:
    native: [run_in_sandbox, github_issue]
  can_delegate_to: []
  sandbox: ephemeral
  secrets: [GITHUB_TOKEN]
  output_type: HealthReport
  limits: { timeout_seconds: 600 }
```

## Appendix B — Example end-to-end flows

**1. "Find value on tonight's AFL."**
Orchestrator (intent→plan) → parallel: Stats specialist (form/fixtures) + Odds specialist
(prices across books) → Modelling agent (win probs) → Value-finder (edges vs market) →
Bankroll/risk (suggested stakes within limits) → Bet-notifier (formats recommendations) →
Concierge (plain-language summary in Slack). User places any bets manually → Bet-tracker
logs them → later Eval agent scores CLV.

**2. "Is everything working?"**
Orchestrator → MCP health/QA agent → sandbox runs `doctor` + contract suite → HealthReport;
on a real break it opens an issue and the Improver drafts a fix PR (CI-gated, human-merged).

**3. "Optimise my DFS lineup for Saturday."**
Orchestrator → Fantasy advisor → Stats specialist (projections/inputs) → sandbox
(optimiser) → lineup + reasoning → Concierge. No betting involved; pure advisory.

**4. "Add Bet365 as a data source."**
Orchestrator → Improver/scout → probes the API in a sandbox → writes a provider spec +
docs + contract row in `sportsdata-mcp` → opens a PR → Reviewer + CI (lint/contract/offline)
→ human merges → QA agent confirms the new feed is healthy.
