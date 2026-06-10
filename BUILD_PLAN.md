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
- [ ] **Dev-disk hygiene:** the dev machine runs tight on space (a full disk killed one session). Use `pip install --no-cache-dir`, clear `~/Library/Caches/pip` and project `dist/`/`build/` periodically, and check `df -h /System/Volumes/Data` before heavy installs.

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
packaged — essentially ready. The pre-flight checks below are **done**; the cloud items are *later*
(phased), and live in the MCP repo.

- [x] **Release tagged** — `sportsdata-mcp` **`v0.1.0`** is tagged + pushed; pin this tag as the dependency here.
- [x] **Per-agent least-privilege scoping** confirmed — `SPORTSDATA_MCP_GROUPS="mlb.reference"` registered only its 20 tools.
- [x] **No-money invariant at source** confirmed — scan of all **335** tools found **zero** placement/deposit/withdraw/stake/account verbs; the one non-GET (`fanduel_racing_promotions`) is a POST that *reads* promos. (Keep the agent-side deny-filter as defense-in-depth.)
- [x] **Capability tags ready** — **51** tags + the `list_tools_by_capability` meta-tool; agent specs reference data by `mcp_capabilities` (resilient to tool renames).
- [x] **Env-var secrets** confirmed — auth providers read `os.environ` (e.g. `DATAGOLF_KEY` via `static_query`); the agent plane injects premium secrets into the MCP subprocess env per workspace.
- [ ] MCP installed into this project (editable path or pinned `v0.1.0`) — do at **M0.4**.

**When deployment matters (it mostly doesn't):**
- **Local (P0–P3):** spawn the MCP as a **local stdio subprocess** — nothing to deploy.
- **Cloud / SaaS (P4):** **co-locate** the MCP in the agents container and spawn it as a subprocess — **still not a separate deployment**. Only the cloud *geo-block* (AU feeds) needs the caching + proxy/egress work below.
- **Hosted-MCP channel (`D23`) — the only separate deployment:** the MCP as a **remote HTTP/SSE server** with auth + rate limits for BYO-LLM users; P3/P4, not a prerequisite.

**MCP-side enhancements — when (phased, all in the `sportsdata-mcp` repo):**
- [ ] **Caching** (per-endpoint TTLs) — at **P2** when the ingestion worker starts polling (reduces upstream load/cost), and required by **P4** (cloud). `D25`.
- [ ] **Proxy / geo-egress** (AU bookmaker feeds geo-block cloud IPs) — at **P4 (cloud deploy)**; for the **P3 public demo**, sidestep by using globally-reachable feeds (MLB/OpenF1/ESPN/cricket). `D25`.
- [ ] **Remote HTTP/SSE transport + auth + rate limits** — at **P3** *only if* the demo backend calls a remote MCP (co-locating a subprocess avoids it), and at **P4** for the **hosted-MCP / BYO-LLM channel**. `D23`.
- [ ] **Re-tag** `sportsdata-mcp` (`v0.x`) whenever its tool surface changes; bump the pin here and let the contract suite + MCP-health agent catch drift.

---

## Phase P0 — Foundations: one real flow end-to-end on a CLI

**Goal:** "Best price + value on tonight's game" works from a CLI, with audit + traces.
**Agents:** Orchestrator + Odds specialist + Stats specialist. **Interface:** CLI. **LLM:** BYO (your key).

### M0.1 — Project scaffolding & tooling ✅
- [x] `pyproject.toml` (hatchling, PEP 621, uv-compatible) with runtime deps (`pydantic`, `pydantic-ai`, `pydantic-settings`, `httpx`, `mcp` client, `litellm`, `sqlalchemy`, `asyncpg`, `alembic`, `typer`, `rich`, `logfire`) + `[dev]` (`ruff`, `mypy`, `pytest`, `pytest-asyncio`, `pre-commit`). *(`uv` not installed locally → used `python -m venv` + pip; project stays `uv sync`-compatible. `sportsdata-mcp` pin deferred to M0.4.)*
- [x] Directory skeleton per `§4` (`gateway/ orchestrator/ agents/ specs/ skills/ mcp/ tools/ sandboxes/ data/ models/ interfaces/ eval/ observability/ operations/`), each a package with `__init__.py`; `py.typed`; minimal Typer CLI (`agents version`).
- [x] `ruff` + `mypy` config (in `pyproject`); `.pre-commit-config.yaml` (ruff, ruff-format, mypy, hooks incl. `detect-private-key`).
- [x] `.github/workflows/ci.yml`: ruff, mypy, `pytest -m "not live and not eval"` on push/PR, Py 3.12 + 3.13.
- [x] `docker-compose.yml` (TimescaleDB pg16); `LICENSE` (proprietary) + `.gitignore` already present.
- [x] **Exit gate:** local — `ruff check` ✓, `mypy` ✓ (15 files), `pytest` ✓ (13 passed), `agents version` → `sportsdata-agents 0.1.0`. CI will mirror.

### M0.2 — Config & secrets ✅
- [x] `config.py`: `Settings` (pydantic-settings, prefix `SPORTSDATA_AGENTS_`) — env, DB URL, `mcp_command`, Logfire token, `default_tenant`/`default_workspace`, local secrets map; cached `get_settings()`.
- [x] `secrets.py`: `SecretRef` (name only), `resolve_secret()` (env → map → `MissingSecretError`), values wrapped in `SecretStr` so they don't leak (§13).
- [x] `workspace.py`: `Workspace`/`Budgets` config object (enabled modules, MCP groups, provisioning mode §8.1, budgets, per-workspace secrets) + `default_workspace()`; `.env.example` added.
- [x] **Exit gate:** `Settings` loads from `.env` (test) + env-var override; secret resolution layering (env > workspace > settings) + missing-secret error tested. ruff/mypy/pytest green (22 passed).

### M0.3 — Data layer (Postgres + Timescale + migrations) ✅
- [x] `data/base.py` (DeclarativeBase + `TimestampMixin` + `TenantScopedModel`, cross-dialect `Uuid`/`JSON`/`Numeric`); `data/db.py` (async engine/session, `session_scope()`, `reset_engine()`).
- [x] `data/models.py` — all **core tables** (`§9`): `tenants`, `workspaces`, `users`, `memberships`, `agent_specs`, `conversations`, `messages`, `agent_runs`, `tool_calls`, `usage_ledger`, `budgets`, `agent_metrics`, `memory`, `notes`, `artifacts`, `recommendations`, `tracked_bets`. Tenant-owned tables inherit `TenantScopedModel`; **`fixtures`/`events`/`selections` are global** (public reference data — the one deliberate exception to tenant-scoping, §9).
- [x] `alembic` (async `env.py` reading `Settings.database_url`; `0001_initial` builds the schema from the model metadata — cross-dialect; Timescale hypertables deferred to M2.1).
- [x] `data/repository.py` — `Repository[T: TenantScopedModel]` + `TenantScope`; **every** query filtered by `tenant_id`+`workspace_id`, every insert stamps them (§12/§13).
- [x] **Exit gate:** `alembic upgrade head` on clean SQLite creates all 20 tables (test); CRUD round-trip (test); **cross-tenant isolation** proven (a tenant-B repo can't `get`/`list` tenant-A's row). `aiosqlite` added to `[dev]`; ruff/mypy clean (22 files), pytest **25 passed**.

> **Remaining §9 tables arrive via new migrations at their milestones** (don't forget the
> migration when you get there): `performance` → **M1.4** · `odds_snapshots`/`prices`
> (Timescale hypertables) → **M2.1** · `models`/`predictions` → **M2.2** · `evals`/`feedback`
> → **M2.4** · `alerts`/`subscriptions` (watches) → **M3.2** · billing
> `subscriptions`/`entitlements` + `leads`/`waitlist` → **P4 / M3.4**.

### M0.4 — MCP client manager ✅
- [x] `mcp/manager.py`: `MCPManager` — spawns `sportsdata-mcp` (v0.2.1, 342 tools) as a **stdio subprocess**, scoped per agent via `SPORTSDATA_MCP_GROUPS` (least privilege, `§13`); async context-manager lifecycle. *(Not installed into this venv — subprocess is the production model; the binary comes from the sibling repo locally / co-located in the container later.)*
- [x] Hard **deny-filter**: `DENY_PATTERN` + `ForbiddenToolError` — money/placement names are hidden from the catalogue *and* refused at `call_tool` before any traffic. Deliberately strict (also hides the read-only `betfair_cashout` feed — accepted cost).
- [x] Catalogue cached per session; `tools_for_capability()` over the `list_tools_by_capability` meta-tool (offline — computed server-side, no upstream HTTP); JSON payload decoding (structured content → text-JSON fallback).
- [x] *(Moved)* the Pydantic AI toolset adapter over this manager lands at **M0.6** with the agent runtime (avoids installing pydantic-ai before it's used).
- [x] **Exit gate:** integration tests (skip when the local binary is absent, e.g. CI): scoping (mlb.reference → only its tools + meta-tools), deny-filter end-to-end (cashout hidden + refused), cross-provider capability lookup (`ref.players` → mlb + openf1). **Live** test: `mlb_teams` round-trip returns the 30 clubs through the spawned server. ruff/mypy clean (23 files); 50 passed + 1 live passed.

### M0.5 — Model gateway (LiteLLM, tiers, BYO/managed seam) ✅
- [x] `models/policy.yaml` + `policy.py`: tier → (primary, fallback) with **per-workspace primary overrides**; task-type → tier routing with a default (`§8`). Packaged as data; `load_policy()` cached.
- [x] `models/gateway.py`: `ModelGateway.complete()` over `litellm.acompletion` — tier-resolved model, **fallback** to the tier's secondary on primary failure, typed `ModelReply` (text/model/tokens/cost).
- [x] **Budgets (`§16.1`)**: `RunBudget` (per-run ceiling from `Workspace.budgets`) — exhausted budgets refused **before** any model traffic (`BudgetExceededError`); cost charged after each call. *(Who sets the ceiling is the §8.1 BYO/managed distinction — by gateway time it's just a number to enforce. Managed platform-key routing is a SaaS-phase seam, noted in the module docstring.)*
- [x] **Metering**: every call emits a `UsageEvent` (model/tier/tokens/cost/latency/tenant/workspace) to a pluggable sink — M0.11 wires it to `usage_ledger`. Cost via `litellm.completion_cost`, **never crashes a run** (unknown pricing → 0).
- [x] **Exit gate:** 10 mock-backend unit tests — policy load/route/override/unknown-tier, primary path + metering, fallback, both-fail, refusal-before-call on exhausted budget, accumulate-and-trip, workspace ceiling, cost-failure safety. ruff/mypy clean; **60 passed**.

### M0.6 — Agent spec schema + loader + lint ✅
- [x] `agents/spec.py`: strict pydantic models for the **agent spec** (`§7`) — all fields (`model_tier` tier-or-explicit-model, `tools{mcp_capabilities, mcp_groups, native}`, `skills`, `forbidden_capabilities`, `can_delegate_to`, `sandbox`, `secrets`, `output_type`, `context{retrieval,long_run,verify}`, `limits{...}`, semver `version` (D27)). `extra="forbid"` everywhere (typos fail loudly); **the no-money invariant is enforced at authoring time** — a spec cannot name a money-ish tool/capability/skill (§13); allowed∩forbidden rejected.
- [x] `specs/_schema.yaml` (self-documenting contract, mirrors the MCP repo convention) + three bundled specs: `orchestrator` (no data tools — delegation only), `odds_specialist`, `stats_specialist` (drafts; M0.8/M0.9 refine prompts + typed outputs).
- [x] `agents/loader.py`: `load_spec_text/file/dir` (skips `_`-files, duplicate-id detection, **errors always carry the source path**), `load_builtin_specs()`, `lint_specs()` (cross-spec: dangling/self delegation). *(Runtime binding — the Pydantic AI agent construction — moved to **M0.7** with the harness, per D28's runtime-neutral spec abstraction; capability→tool validation against the live MCP catalogue happens there too.)*
- [x] CLI: `agents lint [--dir]` (exit 1 on problems) + `agents list`.
- [x] **Exit gate:** bundled specs load + register (test); `agents lint` passes (3 specs); malformed specs fail loudly with the file in the error (id/semver/tier/unknown-field/money-tool/overlap/dup/dangling all tested); CLI lint/list tested. ruff/mypy clean; **77 passed**.

### M0.7 — The harness (loop, loop control, context, skills) — `§8.2` ✅
- [x] **Runtime binding (D28 amended):** custom loop over `ModelGateway` (litellm cross-provider tool-calling — `ModelReply` gained `tool_calls` + `assistant_message`) + `MCPManager`. The spec abstraction stays runtime-neutral; Pydantic AI / Managed Agents can bind behind it later.
- [x] `agents/harness.py`: the loop *gather→plan→act (tool)→observe→verify→stop*; `ToolDef` (JSON-schema'd, async-executed — MCP, native, or sub-agent all fit); tool errors returned to the model as content (never raised), unknown/denied tool names reported back; denied names refused **before** execution + rejected at construction (defense in depth, §13).
- [x] **Loop control:** stop on done(+verifier) / `max_steps` / `max_tool_calls` / budget (refused *before* the next model call) / wall-clock timeout (injectable clock) / **no-progress** (3 identical consecutive tool calls); **§8.1 clamping** — every spec limit min'd with the workspace budgets.
- [x] **Context policy:** token-budget tracking off `tokens_in`; at 70% of the (clamped) window → `compact` policy runs the compactor (deterministic stub: system + recent + marker; LLM-summary later) or `reset` policy stops with `context_exhausted` for an orchestrator hand-off.
- [x] **Skills** (`agents/skills.py`, D29): `SKILL.md` bundles (frontmatter: name/description/triggers + body); **index-only in the system prompt; body disclosed JIT** on trigger match (user input *and* tool results), exactly once; missing/malformed skills fail loudly with the path. Scripts→sandbox lands M1.3; bundles land M0.10.
- [x] **Verification hook (§13.1):** `verifier(answer) → (ok, feedback)`; failure feeds back to the model (1 retry) then reports `verified=False`; real grounding check lands M0.13.
- [x] *(Sub-agent isolation: `ToolDef.execute` wraps another harness's `run()` — composed at M0.8 orchestrator.)*
- [x] **Exit gate:** 20 unit tests — every stop condition, clamping, unknown/denied/raising tools, compaction threshold + reset hand-off, verifier retry/exhaust, skill parse/index/JIT-once/no-trigger/tool-result-trigger. ruff/mypy clean; **104 passed**.

### M0.8 — Orchestrator (runtime + team composition) ✅
- [x] `tools/registry.py` *(pulled forward from M0.10 — the odds spec needs them runnable)*: `implied_probability`, `vig_removal`, `best_price` — deterministic, golden-tested; `get_native_tools()` fails loudly on unknown names.
- [x] `mcp/toolset.py`: the **MCP→ToolDef bridge** — capability tags resolved against the live catalogue via `list_tools_by_capability`; **a zero-tool capability is a loud `CapabilityResolutionError`** (the deferred M0.6 check).
- [x] `agents/runtime.py`: `AgentRuntime` (spec → scoped MCP session + bridged tools + native tools + skills + harness; leak-safe enter/exit) and `open_team()` (orchestrator + specialists one level deep). **Delegation = specialists-as-tools**: each sub-agent runs in its own context and returns a condensed JSON summary (§8.2 isolation, proven by test). Tier-per-agent comes from each spec (`§8`); budgets/ceilings clamp per workspace (M0.7).
- [x] **Data-plane enabler shipped (`sportsdata-mcp` v0.2.2):** `SPORTSDATA_MCP_GROUPS="*"` wildcard — found via integration test: "no groups env" means *nothing* enabled, so capability-only specs resolved to zero tools. Manager now sets `"*"` explicitly when unscoped.
- [x] **Exit gate:** offline — delegation flow end-to-end (orchestrator → specialist → condensed result → synthesis) with isolation assertions; bridge filter/zero-cap/execute tests; native-tool golden values. Integration (real MCP subprocess) — **both bundled specialists' capability tags resolve to real tools** through the wildcard catalogue. Live E2E (real model over real MCP, metered, delegation-asserted) written; **skips without `ANTHROPIC_API_KEY`** — run it when a key is set. ruff/mypy clean; **124 passed**. *(Parallel delegation noted as a later optimization — batch tool calls currently execute sequentially.)*

### M0.9 — First specialists (typed outputs) ✅
- [x] *(Specs shipped at M0.6, live-proven at M0.8 — this milestone delivered the missing piece: typed outputs.)*
- [x] `agents/outputs.py`: registered result schemas — `OddsComparison` (quotes/best/fair_probability/sources) + `StatsAnswer` (answer + sourced facts); `OUTPUT_TYPES` registry (loud on unknown); `parse_output` (fence/prose-tolerant JSON extraction → pydantic validation); `schema_instructions`. **Portable**: plain JSON-in-text, no vendor structured-output APIs.
- [x] Harness enforcement: `output_type` resolved at construction (loud); schema instructions appended to the system prompt; final answers parsed with **one format-feedback retry**, then surfaced honestly (`parsed=None`, raw text kept). `RunResult.parsed` carries the validated instance.
- [x] `odds_specialist` → `OddsComparison`; `stats_specialist` → `StatsAnswer`; orchestrator stays free-text (synthesis). Lint cross-checks `output_type` against the registry.
- [x] **Exit gate:** offline — registry/parse formats (bare/fenced/prose)/schema-error/harness parse/feedback-retry/give-up/unknown-type/lint, 12 tests. **Live** — `stats_specialist` answered a scoped question via the real MCP with a **validated `StatsAnswer`** (Yankees, sources cited). ruff/mypy clean; offline **147 passed**, live **3 passed**.

### M0.10 — Native tools + first skills ✅
- [x] `tools/registry.py`: `vig_removal`, `implied_probability`, `best_price` *(shipped at M0.8)* + **`expected_value`** (p·odds−1, `is_value` flag) and **`kelly_fraction`** (Kelly-optimal bankroll fraction, clamped ≥0; deliberately named `_fraction` not `_stake` — a money-verb name would rightly trip the deny-filter; informational only, §14). Golden-tested incl. bounds. *(DB helpers belong with M0.11 persistence.)*
- [x] `skills/`: first two bundles — **`vig_removal`** (procedure: full market → `vig_removal` → sharpest-book fair prob → `expected_value`; sanity anchors; partial-market rule) and **`compare_odds`** (identify event once → per-book quotes with fetch times → `best_price` → EV; same-line rule; advisory line). Triggers tuned against false positives ("margin" excluded — winning-margin markets; word-boundary matching from M0.7).
- [x] `odds_specialist` granted `expected_value` + `skills: [vig_removal, compare_odds]`; lint green.
- [x] **Exit gate:** golden values for every native tool (incl. negative-edge Kelly clamp, probability bounds, deny-filter naming test); builtin bundles load from the packaged root, trigger on the right phrases, refuse false positives; **end-to-end**: the odds specialist's skills disclose JIT in a harness run with index-in-prompt + both bodies disclosed + typed output still parsing. ruff/mypy clean; offline **157 passed**, live **3 passed**, `agents lint` ✓. *(Skill scripts run in the sandbox from M1.3.)*

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
- [ ] **Agent-builder** — NL → a validated agent/module spec (the customization path, §7.1); drafts the system prompt, skills, data (capability tags), tier, schedule, and limits from a plain-English goal; preview/test before save; output is versioned (D27).
- [ ] **Capability→friendly-label map** — human names for capability tags + skills/modules ("AFL stats", "Compare odds across books") so users pick from a curated catalogue, never raw tool names (§7.1). Reused by the visual builder (M4.5).
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
- [ ] **(P1)** Add a Postgres service container job to CI so migrations + queries are tested on the prod dialect, not just SQLite (JSON/JSONB, timezone semantics).
- [ ] **(P3)** Enable branch protection on `main` (PRs + CI required) before the engineering agents exist — they must be unable to push directly.

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
