# sportsdata-agents — Technical Build Plan (tick-off checklist)

A granular, technical companion to [`PLAN.md`](./PLAN.md) (the architecture). This is the
**execution checklist**: work top-to-bottom, tick `- [ ]` → `- [x]` as you go. Each milestone
ends with an **Exit gate** — don't start the next until it's green.

References: `§N` = PLAN.md section, `Dn` = PLAN.md decision register entry.

---

## Conventions & ground rules

- [ ] **Language/runtime:** Python 3.12+ (match `sportsdata-mcp`).
- [ ] **Package manager:** `uv` (fast, lockfile). Project is `sportsdata_agents`.
- [ ] **Layout:** `src/sportsdata_agents/...` per `§4`; one-directional deps (interfaces → gateway → orchestrator → agents → {mcp, tools, skills, sandboxes, data, models}).
- [ ] **Style/lint:** `ruff` (format + lint), `mypy` (typed), line length 120 — mirror the MCP repo.
- [ ] **Tests:** `pytest` + `pytest-asyncio`; markers `unit`, `integration`, `live`, `contract`, `eval`. Default CI runs `-m "not live and not eval"`.
- [ ] **Config:** pydantic-settings; everything via env/`.env` (never commit secrets). `tenant_id`/`workspace_id` threaded from day one (`§12`).
- [ ] **Branching:** feature branches → PR → CI green → review → merge `main`. Engineering agents only ever open PRs (`§15`).
- [ ] **Definition of Done (every task):** code + types + unit test + docstring + passes ruff/mypy/pytest; no secret in tree.
- [ ] **Commit trailer:** `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` when AI-assisted.

### Prerequisites / accounts (gather before P0)
- [ ] Anthropic API key (+ OpenAI and/or Google) — for the model pool (`D12`).
- [ ] Local Postgres 16 + TimescaleDB extension (Docker compose).
- [ ] `sportsdata-mcp` checked out locally and runnable (`uvx`/editable) — the data plane.
- [ ] Logfire account (or self-host) for tracing (`D8`).
- [ ] (P1+) E2B **or** Modal account for sandboxes (`D5`).
- [ ] (P1+) Slack workspace + app for the Slack adapter.
- [ ] (P4) Stripe account; auth provider (Clerk/Auth0/Supabase); Vercel/Fly.

---

## Data-plane (`sportsdata-mcp`) readiness — confirm before/early P0

**No deployment is required to start.** The agent plane spawns `sportsdata-mcp` as a **local stdio
subprocess** and talks to it directly. The MCP is built, contract-tested, capability-tagged, and
packaged — essentially ready. Confirm the items below; the cloud items are *later*, and live in the
MCP repo.

- [ ] MCP runs locally (`sportsdata-mcp serve`, stdio) and is installable into this project (editable / `uvx` from the private repo).
- [ ] **Tag a release/version** of `sportsdata-mcp` to pin as a dependency here (reproducible builds).
- [ ] Agent specs reference data tools by **capability tag** (`mcp_capabilities`), not raw tool names — resilient to MCP tool renames.
- [ ] Per-agent **least-privilege scoping** via `SPORTSDATA_MCP_GROUPS` confirmed from a spawned subprocess.
- [ ] **No-money invariant at source** confirmed — the MCP exposes no placement/deposit/account tools (pure data + odds reading); keep the agent-side deny-filter anyway.
- [ ] Premium-provider secrets (e.g. `DATAGOLF_KEY`) pass into the MCP subprocess env per workspace.
- [ ] **Later (cloud/SaaS, MCP repo):** response **caching** (per-endpoint TTLs) + **proxy/geo-egress** (AU bookmaker feeds geo-block cloud IPs) — `D25`. Prefer global feeds (MLB/OpenF1/ESPN/cricket) for the public demo.
- [ ] **Later (hosted-MCP channel, `D23`):** deploy the MCP as a **remote HTTP/SSE server** with auth + rate limits — only for BYO-LLM users plugging into their own Claude/ChatGPT; not a prerequisite.

---

## Phase P0 — Foundations: one real flow end-to-end on a CLI

**Goal:** "Best price + value on tonight's game" works from a CLI, with audit + traces.
**Agents:** Orchestrator + Odds specialist + Stats specialist. **Interface:** CLI. **LLM:** BYO (your key).

### M0.1 — Project scaffolding & tooling
- [ ] `uv init`; `pyproject.toml` (deps: `pydantic`, `pydantic-ai`, `pydantic-settings`, `httpx`, `fastmcp`/`mcp` client, `litellm`, `asyncpg`/`sqlalchemy`, `alembic`, `typer`, `rich`, `logfire`, `pytest`, `pytest-asyncio`, `ruff`, `mypy`).
- [ ] Directory skeleton per `§4` (`gateway/ orchestrator/ agents/ specs/ skills/ mcp/ tools/ sandboxes/ data/ models/ interfaces/ eval/ observability/ operations/`), each with `__init__.py`.
- [ ] `ruff` + `mypy` config; `pre-commit` hooks.
- [ ] `.github/workflows/ci.yml`: ruff, mypy, `pytest -m "not live and not eval"`, on push/PR (mirror MCP repo).
- [ ] `LICENSE` (proprietary), `.gitignore`, `docker-compose.yml` (Postgres+Timescale).
- [ ] **Exit gate:** `uv run pytest` (empty), `ruff check`, `mypy` all green in CI.

### M0.2 — Config & secrets
- [ ] `config.py`: `Settings` (pydantic-settings) — DB URL, model keys, MCP location, Logfire token, `default_tenant`.
- [ ] Secret resolution: env first, then a `secrets` map; a `SecretRef` type (name → value) so specs never hold secrets (`§13`).
- [ ] `Workspace`/`Tenant` config object (enabled modules, MCP groups, model policy, provisioning mode, budgets) — even with one local workspace.
- [ ] **Exit gate:** `Settings` loads from `.env`; unit test for secret resolution + missing-secret error.

### M0.3 — Data layer (Postgres + Timescale + migrations)
- [ ] `data/db.py`: async engine/session (SQLAlchemy 2.0 async or asyncpg + repositories).
- [ ] `alembic` init; first migration with **core tables** (`§9`), all carrying `tenant_id`/`workspace_id`:
  - [ ] `tenants`, `workspaces`, `users`, `memberships`
  - [ ] `agent_specs` (registry; built-ins from files, custom in DB)
  - [ ] `conversations`, `messages`
  - [ ] `agent_runs`, `tool_calls` (audit)
  - [ ] `usage_ledger`, `budgets`, `agent_metrics` (`§16.1`)
  - [ ] `memory`, `notes`, `artifacts` (`§8.2` external memory)
  - [ ] `fixtures`, `events`, `selections` (normalised entities — minimal now)
  - [ ] `recommendations`, `tracked_bets` (distinct — `§9`)
- [ ] Repository layer enforcing `tenant_id` scoping on every query (the SaaS seam, `§12`).
- [ ] **Exit gate:** `alembic upgrade head` on a clean DB; round-trip CRUD test per table; a test proving cross-tenant queries are filtered.

### M0.4 — MCP client manager
- [ ] `mcp/manager.py`: open an MCP client session to `sportsdata-mcp` (stdio/uvx locally), **scoped per agent** via `SPORTSDATA_MCP_GROUPS` (least privilege, `§P3`/`§13`).
- [ ] Map MCP **capability tags** → the tools an agent may call; build a Pydantic AI toolset from the scoped session.
- [ ] Session lifecycle (open/close), retry/reconnect, and a hard **deny-filter** that strips any money/placement tool (`§13` no-money invariant).
- [ ] Cache the tool catalogue; surface `list_tools_by_capability` for the orchestrator.
- [ ] **Exit gate:** integration test (`live`-ish, local MCP) — an agent calls `mlb_teams`/an odds tool through the manager and gets data; deny-filter test proves no placement tool is exposed.

### M0.5 — Model gateway (LiteLLM, tiers, BYO/managed seam)
- [ ] `models/gateway.py`: wrap LiteLLM; `complete(messages, tier, workspace)` resolving **tier → concrete model** via `models/policy.yaml` (`§8`), with fallback on error/rate-limit.
- [ ] Provisioning modes (`§8.1`/`D24`): **BYO** (workspace keys) vs **managed** (platform keys + hard caps). Caps **clamped** to the mode.
- [ ] Emit per-call cost/tokens → `usage_ledger` (`§16.1`).
- [ ] **Exit gate:** unit tests with a mock backend — tier resolution, fallback, BYO vs managed key selection, cost row written, managed cap enforced (run refused over ceiling).

### M0.6 — Agent runtime + spec loader
- [ ] `specs/_schema.yaml` + pydantic models for the **agent spec** (`§7`): `id, display_name, model_tier, system_prompt, tools{mcp_capabilities, mcp_groups, native}, skills, forbidden_capabilities, can_delegate_to, sandbox, secrets, output_type, context{retrieval,long_run,verify}, limits{max_tool_calls,max_steps,max_tokens,timeout,cost_ceiling}, spec_version + semantic version`.
- [ ] `agents/loader.py`: load + validate specs (files + DB), build a Pydantic AI `Agent` (model, system prompt, scoped toolset, output type, deps).
- [ ] `lint` command: validate all specs (mirror `sportsdata-mcp lint`).
- [ ] **Exit gate:** load the bundled specs; `lint` passes; a malformed spec fails loudly; registration test (all expected agents present).

### M0.7 — The harness (loop, loop control, context, skills) — `§8.2`
- [ ] `agents/harness.py`: the agent loop *gather→plan→act→observe→verify→stop*.
- [ ] **Loop control:** stop on goal+verifier / `max_steps` / budget/time/token ceiling / awaiting-human; **no-progress/thrash** detector.
- [ ] **Context policy:** `retrieval: jit`; **compaction** hook; **context-reset/hand-off** path; budget tracking (warn as window fills).
- [ ] **Skills loader** (`skills/`, `D29`): discover skill bundles, **progressive disclosure** (load instructions JIT when relevant), run skill scripts in the sandbox (stub until M1.x), keep context lean.
- [ ] **Sub-agent isolation:** delegated agents run in their own context, return a condensed summary.
- [ ] **Exit gate:** unit tests — loop stops on each condition; max_steps respected; a skill is loaded only when its trigger matches; compaction fires past a token threshold (mock).

### M0.8 — Orchestrator
- [ ] `orchestrator/`: intent classify → plan → delegate (parallel where independent) → synthesise; agents-as-tools delegation.
- [ ] Model-selection: pick a **tier per task** (`§8`); enforce guardrails (no-money invariant; advisory-only).
- [ ] Per-run budget/latency ceilings from the workspace (`§12.1`/`§16.1`).
- [ ] **Exit gate:** "find value on tonight's game" decomposes into Stats + Odds calls and synthesises; trace shows the plan + delegations.

### M0.9 — First specialists
- [ ] `specs/odds_specialist.yaml` (`sport.prices`, `sport.event_markets`; native `vig_removal`, `implied_probability`; output `OddsComparison`).
- [ ] `specs/stats_specialist.yaml` (data groups; output `StatsAnswer`).
- [ ] `specs/orchestrator.yaml`.
- [ ] **Exit gate:** each specialist answers a scoped question via the MCP with correct typed output.

### M0.10 — Native tools + first skills
- [ ] `tools/`: `vig_removal`, `implied_probability`, `best_price`, DB helpers — deterministic, unit-tested.
- [ ] `skills/`: first 1–2 skill bundles (`vig-removal` playbook; a `compare-odds` walkthrough) with `SKILL.md` + script.
- [ ] **Exit gate:** golden-value unit tests for each native tool; a skill runs end-to-end (script path stubbed/local).

### M0.11 — Observability & cost
- [ ] `observability/`: wire **Logfire** (or OTel) — trace every agent run, delegation, tool call, model choice, tokens, latency, cost.
- [ ] Persist `agent_runs` + `tool_calls` + `usage_ledger` on every run.
- [ ] **Exit gate:** one CLI run produces a full trace + DB audit rows + a cost row.

### M0.12 — CLI interface
- [ ] `interfaces/cli/` (Typer): `agents chat`, `agents run "<prompt>"`, `--workspace`, streaming output via `rich`.
- [ ] Channel-agnostic message in/out (so Slack reuses it).
- [ ] **Exit gate:** the headline flow works from the CLI with streamed answer + sources.

### M0.13 — Accuracy & provenance (`§13.1`/`D26`)
- [ ] Tool results carry `{provider, endpoint, fetched_at, snapshot_id}`; agents cite source+timestamp per figure.
- [ ] **Grounding post-check:** validator extracts numeric/factual claims from the draft and checks them against tool outputs (+ sandbox results); ungrounded → flag/regenerate.
- [ ] Explicit "no data" path; "informational, not advice" disclaimer; **no edge/profit language** (`§14`).
- [ ] **Exit gate:** test — an answer with a fabricated number is caught by the grounding check; a grounded answer passes.

### M0.14 — Tests & CI hardening
- [ ] Unit coverage for tools/gateway/loader/harness; integration test for the headline flow (local MCP).
- [ ] First **eval** case (`-m eval`): a golden Q→A graded for factual accuracy.
- [ ] **🚪 P0 EXIT GATE:** From a clean machine — `docker compose up`, `alembic upgrade head`, `agents run "best price + value on <real game>"` returns a sourced, grounded answer; full trace + audit + cost recorded; CI green.

---

## Phase P1 — Track & converse: Slack, performance, first sandbox

**Goal:** log a user's bets, report ROI/CLV in Slack; one analysis runs in a sandbox.

### M1.1 — Gateway service
- [ ] `gateway/` FastAPI: channel-agnostic `POST /message`, auth middleware (no-op locally), tenant resolution, rate/cost limits, **sync + async (task)** runs, SSE streaming, audit.
- [ ] Task queue (Arq/Celery + Redis) for long runs; run status + resume hooks.
- [ ] **Exit gate:** CLI and a test client both drive the gateway; async run returns a task id + streams status.

### M1.2 — Slack adapter (`D4`)
- [ ] `interfaces/slack/` (Bolt): events→gateway, threaded replies, slash commands, **push notifications**, OAuth install.
- [ ] Map a Slack thread → a conversation/session.
- [ ] **Exit gate:** ask a question in Slack, get a streamed threaded answer; a push alert can be delivered.

### M1.3 — Sandbox integration (`D5`, `§10`)
- [ ] `sandboxes/base.py`: `Sandbox` interface `run(code, files, network_policy) → result`.
- [ ] E2B (or Modal) backend; per-run isolation; secret injection per-run; **allow-listed egress**; resource/time caps.
- [ ] Wire skills' scripts + the data-analysis agent to the sandbox.
- [ ] **Exit gate:** an agent runs Python in the sandbox (pandas) and returns a verified result; egress allow-list enforced.

### M1.4 — Reporting / tracking agents (`§6` Tier 3, advisory-only)
- [ ] **Bet-notification agent** (`specs/bet_notifier.yaml`) — formats recommendations (selection, suggested stake, book, reasoning); `forbidden_capabilities` deny-list; **never places**.
- [ ] **Bet-tracking / P&L agent** — log a user's placed bets (manual/confirmation), settle from results feeds, compute P&L/ROI/**CLV**, hit-rate by market/sport. Writes `tracked_bets`, `performance`.
- [ ] **Bankroll / risk manager** — Kelly/flat staking, exposure & correlation limits; **gate before any recommendation is surfaced**.
- [ ] **Concierge** — plain-language synthesis; owns per-channel UX.
- [ ] **Exit gate:** log 3 bets → settle → ROI + CLV reported in Slack; risk manager caps a stake.

### M1.5 — Memory service (`§8.2`)
- [ ] `memory` read/write API (user prefs, long-term facts, structured notes/to-dos, artifacts); JIT recall in the harness; pgvector for semantic recall (`D11`) when needed.
- [ ] **Exit gate:** a preference set in one session is recalled in the next; notes persist across a context reset.

### M1.6 — Data-analysis agent
- [ ] `specs/data_analysis.yaml` (sandbox: ephemeral) — ad-hoc analysis + charts to `artifacts`/object store.
- [ ] **Exit gate:** "chart X's form last 10 games" produces a chart + grounded commentary.

- [ ] **🚪 P1 EXIT GATE:** Slack live; bet tracking + CLV reporting works; one sandboxed analysis runs; all advisory-only invariants tested.

---

## Phase P2 — Quant: models, value, backtests, ingestion

**Goal:** a model backtests with CLV > 0 on held-out data; value alerts fire.

### M2.1 — Ingestion worker + odds-history warehouse (`§9.1`/`D25`)
- [ ] `operations/ingestion/` (or sibling service): scheduled jobs call MCP tools at intervals → write `odds_snapshots`, `prices` to **TimescaleDB** hypertables.
- [ ] Backfill + retention policies; dedupe; per-provider schedules; failure handling → triage (M3.x).
- [ ] **Exit gate:** continuous capture of a market over time; query line movement for an event.

### M2.2 — Modelling agent
- [ ] `specs/modelling.yaml` (sandbox + history store) — build/run models; output **calibrated** probabilities; persist `models`, `predictions` with calibration metadata.
- [ ] Skill bundles: `build-a-totals-model`, `calibrate-probabilities`.
- [ ] **Exit gate:** a model produces calibrated probs on a holdout; calibration (Brier/log-loss) recorded.

### M2.3 — Value-finder + backtesting
- [ ] **Value-finder** — model prob vs market (vig-removed) → +EV, edge %, fair odds (deterministic math/tools).
- [ ] **Backtesting agent** — replay `odds_snapshots` + results → ROI/CLV/variance.
- [ ] **Exit gate:** backtest reports CLV>0 on held-out data for a sample strategy; value alerts computed.

### M2.4 — Eval harness (`§16.3`)
- [ ] `eval/` runner (`-m eval`): calibration, **CLV** (gold metric), routing efficiency, answer-accuracy; golden datasets; LLM-judge + deterministic source-match.
- [ ] Dashboards/reports; gate "is this change better?".
- [ ] **Exit gate:** eval suite runs in CI (scheduled), produces scores, fails a deliberately-worse change.

- [ ] **🚪 P2 EXIT GATE:** end-to-end quant loop (ingest → model → value → backtest → eval) green.

---

## Phase P3 — Self-maintaining + alerts + fantasy + GTM

**Goal:** ops agents maintain the repos; alerts fire; fantasy works; the public demo is live.

### M3.1 — Operations plane (`§3.1`, platform-only)
- [ ] Separate **operations deployable** + operator console/CLI; platform creds (GitHub/CI) **never** in tenant runtime.
- [ ] **MCP health/QA agent** — run `doctor` + the MCP contract suite on a schedule; file issues on real breaks.
- [ ] **Repo-improver / scout** — propose changes from feedback; **open PRs only** (git + GitHub API).
- [ ] **Code-reviewer agent** — review PRs; approve/request changes; **human merges**.
- [ ] **Eval / benchmark agent** — scheduled + PR-triggered; writes `evals`/`agent_metrics`.
- [ ] **Incident-triage agent** — watch errors/alerts; auto-remediate within a safe allow-list (retry, fail over provider, disable a broken module) else **escalate a report to the operator**.
- [ ] Aggregated/anonymized signals only cross product→operations (`§3.1`/`D16`).
- [ ] **Exit gate:** QA/triage catch a broken feed (auto-fix or escalate); improver lands a CI-passing PR a human merges.

### M3.2 — Line-monitor / alerting
- [ ] Standing watches (line moves, steam, scratchings, value appear/vanish) on the ingestion stream → push alerts (Slack/Discord); durable/resumable (`§8.2`).
- [ ] `alerts`, `subscriptions` tables.
- [ ] **Exit gate:** a configured watch fires a push alert on a real line move.

### M3.3 — Fantasy advisor + agent-builder + Discord
- [ ] **Fantasy advisor** — projections, lineup optimisation (sandbox), player research.
- [ ] **Agent-builder** — NL → a validated agent/module spec (the customization path).
- [ ] **Discord adapter**.
- [ ] **Exit gate:** optimise a DFS lineup; a user builds a working custom agent from chat.

### M3.4 — Marketing site + live MCP demo (`§11.1`)
- [ ] Astro/Next site (`D21`): hero, **live MCP chat demo** (`D22` hybrid — curated prompts → real read-only, rate-limited+budget-capped demo agent, tool calls shown live; animated-playback fallback), "works with any LLM", **live capability counters** from the MCP, per-persona use cases, pricing, docs, sign-up; `leads` capture.
- [ ] Hosted/remote-MCP channel (`D23`) for BYO-LLM.
- [ ] **Exit gate:** public site live; demo runs a real bounded query with visible tool calls; no secret/abuse exposure.

### M3.5 — Spec/module versioning (`§7`/`D27`)
- [ ] Semantic version per agent/module spec; workspaces **pin** versions; migration path + deprecation window; schema-version guard.
- [ ] **Exit gate:** bump a module version without breaking a workspace pinned to the old one; migration applies on opt-in.

- [ ] **🚪 P3 EXIT GATE:** self-improvement loop demonstrably closes (perf/feedback → PR → CI → review → merge); alerts + fantasy + demo live.

---

## Phase P4 — Productize (SaaS) — gated on go/no-go + legal (`D13`)

**Goal:** a second tenant on a paid tier with isolated data, enforced entitlements + budgets.

### M4.1 — Multi-tenancy hardening
- [ ] Postgres **Row-Level Security** on; per-tenant isolation tests (a tenant cannot read another's rows).
- [ ] Per-workspace secrets in **Vault/cloud KMS**; BYO keys vs platform keys separated (`§8.1`).
- [ ] **Exit gate:** isolation test suite green; secrets never in DB/logs.

### M4.2 — Auth + accounts
- [ ] Clerk/Auth0/Supabase; orgs/workspaces/seats; RBAC (operator vs member); SSO option for enterprise.
- [ ] **Exit gate:** sign-up → workspace → invite a seat → scoped access.

### M4.3 — Billing, tiers & entitlements (`§12.1`)
- [ ] **Stripe** subscriptions + **metered usage** fed by `usage_ledger`.
- [ ] `subscriptions`, `entitlements` tables; gateway checks entitlements **before** enabling an MCP/agent/interface/module or starting a run.
- [ ] Tiers (T1/T2/T3) + add-ons (modules, MCPs, seats, custom build, managed-LLM); **hybrid cost recovery** (allowance + metered overage + hard budgets, `D19`).
- [ ] **Unit-economics dashboard** — populate `c_run → COGS_user → gross margin` from real telemetry (`§12.1`).
- [ ] **Exit gate:** upgrade/downgrade changes entitlements live; overage metered; a tier's margin is visible.

### M4.4 — Module catalogue & entitlement gating (`D16`)
- [ ] Operator-authored **module specs** (bundle agents + skills + MCP groups + config + UI); customer selects per workspace; **Trading/Betting** module jurisdiction-gated (`§14`).
- [ ] **Exit gate:** enable/disable a module flips the workspace's capabilities; betting module gated by jurisdiction entitlement.

### M4.5 — Web app + **agent/module management console** (`§11`, `D30`)
The web app is also the **control panel** where users compose and run their agent team (the
non-technical path to everything that's specs+chat in P0–P3). Sub-surfaces:
- [ ] **Chat workspace** — the conversational product (same gateway as CLI/Slack), streamed, with tool-call/provenance display.
- [ ] **Module catalogue** — browse, enable/disable, and configure modules (within entitlements; Trading/Betting jurisdiction-gated).
- [ ] **Agent management** — view/edit agent specs within entitlements (prompt, tools, skills, model tier, limits); enable/disable; per-agent **cost & performance** from `agent_metrics`.
- [ ] **Visual custom-agent builder** — a UI wrapping the agent-builder agent (NL → validated, versioned spec); save as a custom module.
- [ ] **Provisioning & budgets** — BYO-LLM keys vs managed (`§8.1`), per-agent/workspace caps + budgets, usage meter.
- [ ] **Dashboards** — P&L / ROI / CLV, odds/line-movement viz, run history + audit, alerts/subscriptions management.
- [ ] **Billing** — plan/tier, add-ons, invoices, usage (Stripe, `§12.1`).
- [ ] **Guided onboarding** for non-technical users (`§11`): wizard → pick module/bundle → provisioning → sample prompts.
- [ ] **Exit gate:** a non-technical user, via the web app, enables a module, builds/edits an agent, sets a budget, runs a query, and sees its cost/performance — reaching first value in minutes.

> **Earlier (optional, P1+):** a thin **internal admin UI** for *you* (the operator) to manage
> workspaces/specs/budgets without editing files. Nice-to-have; specs + CLI + agent-builder suffice
> until the full console at P4.

### M4.6 — Ops readiness (`§17`)
- [ ] Managed Postgres+Timescale (backups/DR), autoscaled compute (Fly/Railway/cloud), SLOs.
- [ ] **Status page** (feed/agent/uptime, fed by observability); incident response (triage agent + human on-call).
- [ ] Security pass: pen-test the public demo + hosted-MCP + gateway; multi-tenant isolation review; (optional) SOC2 prep, DPAs, data-retention/export/delete.
- [ ] **Exit gate:** status page live; DR restore tested; isolation + secrets review signed off.

- [ ] **🚪 P4 EXIT GATE:** a second paying tenant fully isolated, entitlements + budgets enforced, versioned modules, disclaimers, status page.

---

## Cross-cutting tracks (continuous, every phase)

### Testing
- [ ] Unit (tools, gateway, harness, loader) · integration (flows vs local MCP) · contract (agent registration + typed-output shape) · eval (accuracy/calibration/CLV) · isolation (multi-tenant).
- [ ] CI default `-m "not live and not eval"`; nightly job runs `live` + `eval`.

### Security & guardrails (`§13`)
- [ ] No-money invariant test on every agent (deny-filter). · Prompt-injection handling (untrusted feed/web content). · Plane isolation (no platform creds in tenant runtime). · Secret-in-tree scan in CI. · Per-run + per-tenant budget ceilings enforced.

### Observability & cost (`§16`)
- [ ] Trace coverage on every new agent/tool. · `usage_ledger` populated. · `agent_metrics` rollups (cost/successful-task, success rate, value-add, quality, latency) — retire/retune weak agents.

### Docs
- [ ] Keep `PLAN.md` ↔ `BUILD_PLAN.md` in sync. · Per-agent + per-module README. · Operator runbook (incidents, deploys, migrations). · Customer docs (connect MCP, modules, onboarding).

### Harness hygiene (`§8.2`)
- [ ] Periodically **stress-test harness assumptions** — remove scaffolding the model no longer needs as models improve; the eval agent measures whether each component earns its keep.

---

## Suggested first-week slice (smallest end-to-end vertical)
1. [ ] M0.1 scaffold + CI · 2. [ ] M0.2 config · 3. [ ] M0.3 minimal DB (`agent_runs`, `usage_ledger`) · 4. [ ] M0.4 MCP manager (one provider) · 5. [ ] M0.5 model gateway (one model) · 6. [ ] M0.6 spec loader + one agent · 7. [ ] M0.7 minimal loop · 8. [ ] M0.12 CLI · 9. [ ] M0.11 tracing → **a single agent answers one real sports question from the CLI with a trace and a cost row.** Everything else builds outward from that vertical.
