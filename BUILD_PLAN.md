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

### M0.11 — Observability & cost ✅
- [x] `observability/recorder.py`: `RunRecorder` protocol + `DbRecorder` — every run → `agent_runs` (status from stop_reason, cost/tokens/model/tier/latency/finished_at), every tool call → `tool_calls` (args/ok/latency), every gateway `UsageEvent` → `usage_ledger` (buffered per run via the sink, flushed in one transaction at run end). **Recording can never break a run** — the harness guards every hook (tested with an exploding recorder).
- [x] **Delegation audit tree:** `CURRENT_RUN_ID` contextvar (same pattern as the shared budget) → sub-runs persist `parent_run_id`; child rows carry **delta** cost, parent the team total (the M0.8-review accounting, now proven in the DB).
- [x] `agent_runs.parent_run_id` added to the model + **inspector-guarded migration 0002** (0001 builds from live metadata, so fresh DBs already have the column — guard tested).
- [x] `observability/tracing.py`: `setup_observability()` — stdlib logging always; Logfire enabled when `SPORTSDATA_AGENTS_LOGFIRE_TOKEN` is set (the recorder's structured run/tool/usage log lines ride it); setup failure can't break the app.
- [x] Recorder threaded through `Harness` / `AgentRuntime` / `open_team`.
- [x] **Exit gate:** a recorded run produces run + tool + 2 usage rows with correct tenant scoping, cost (0.004), tokens, model; delegation produces the parent/child tree with delta accounting; failed tools persist `ok=False`; migration idempotent on fresh DBs. *(The "one CLI run" wording lands with M0.12, which wires this recorder into the CLI.)* ruff/mypy clean; offline **166 passed**, live **3 passed**.

### M0.12 — CLI interface ✅
- [x] `gateway/service.py`: **`TeamSession`** — the channel-agnostic seam (Slack reuses it at M1.2): owns specs, gateway, MCP pool, recorder, and the opened team (or one agent via `agent_id`); leak-safe open/close; `detect_tier_overrides()` (BYO-LLM §8.1 — first configured key of ANTHROPIC/OPENROUTER/GEMINI/GROQ/OPENAI pins the tiers, shared with the live tests' logic).
- [x] `interfaces/cli/`: **`agents run "<prompt>"`** + **`agents chat`** (warm REPL; `/exit`; turns independent until the memory service) with `--workspace` + `--agent`; `.env` loaded at bootstrap; `setup_observability()` (now also quiets litellm/httpx INFO noise so the recorder's lines are readable).
- [x] `interfaces/cli/progress.py`: `ConsoleProgressRecorder` — live delegation/tool lines (✓/✗ + latency) wrapped around the `DbRecorder` (printing additive; persistence untouched; **DB-optional** — audit degrades to a warning when Postgres is down via `try_db_recorder`).
- [x] Rendering: typed answer preferred over raw text; sources line; stop/steps/tools/cost/verified footer.
- [x] **Exit gate (run for real):** `agents run "...Aaron Judge..."` → opened the team, delegated, hit live MLB data, and answered "**New York Yankees**" — and *honestly declined* the manager question (outside its capability scope) instead of hallucinating. Progress lines + footer rendered. *(Token-level streaming awaits gateway `stream=` support; progress streaming shipped.)* Fixed en route: litellm's import-time `load_dotenv` polluting Settings-defaults test; CLI provider detection (the policy's Anthropic default failed with only an OpenRouter key). ruff/mypy clean; offline **179 passed**, live **3 passed** (one transient LLM-nondeterminism flake on the E2E, passed on re-run — noted).

### M0.13 — Accuracy & provenance (`§13.1`/`D26`) ✅
- [x] **Provenance envelope:** every bridged MCP result wrapped as `{_source: {tool, fetched_at}, data}` — citable source + timestamp per figure (`snapshot_id` arrives with the ingestion worker, M2.1).
- [x] **Grounding post-check** (`agents/grounding.py`, deterministic — no LLM judging an LLM): numeric claims extracted from the draft (commas/decimals/leading-dot normalized, single-digit ints skipped as noise) must appear in the run's **evidence** (user input + tool results); ungrounded → one feedback retry → `verified=False` honestly. **Auto-wired** whenever `context.verify` is true (all bundled specs) — `Verifier` signature now `(answer, evidence)`.
- [x] **Evidence hygiene** (found by the exit-gate test): harness-injected messages (verifier feedback — which *quotes* the fabricated number and would self-launder it — plus `[format]`/skill bodies/compaction markers) are **excluded** from evidence.
- [x] "No data" path lives in the specs' prompts (observed live: the team declined the manager question rather than guessing) + the verifier's "say the data is unavailable" feedback; **advisory disclaimer** on every CLI answer footer; no edge/profit language (tested against the §14 banned list).
- [x] **Exit gate:** fabricated 62-HRs answer caught → feedback → corrected 58 passes `verified=True`; persistent fabrication reported `verified=False`; echoed-user-numbers/no-numbers/no-evidence cases all covered; default wiring on/off tested. Two self-bugs caught by the gate itself: `%g` scientific-notation normalization and the feedback-poisoning loop. ruff/mypy clean; offline **193 passed**, live **3 passed with grounding active**.

### M0.14 — Tests & CI hardening ✅ (exit-gate live demo pending a funded model key)
- [x] Coverage: **196 offline tests** across config/secrets/data/repository/MCP manager+pool/gateway+policy/specs+loader/harness/skills/outputs/grounding/orchestration/persistence/CLI; headline-flow integration (real MCP subprocess) + 3 live tests (typed output, delegation E2E, mlb roundtrip), all previously green with grounding active.
- [x] First **eval** cases (`tests/eval/test_golden.py`, `-m eval`): golden stats fact (live data, grounded, delegation asserted) + golden odds math (exact 0.4/40%, `verified=True`). *Deterministically graded; the seed of M2.4's harness.*
- [x] README: full quickstart (both repos, .env incl. free-tier keys, optional compose→alembic, CLI usage), testing matrix, honest status.
- [x] **🚪 P0 EXIT GATE — CLOSED** (2026-06-10, Anthropic key): CI green ✓ · **both golden evals pass** (stats fact through the full team, grounded + delegated; odds math exact 0.4 `verified=True`) ✓ · audit/trace/cost rows proven (M0.11–M0.12) ✓ · grounded+sourced live team answers (M0.12–M0.13) ✓ · honest-refusal + budget-tripwire behaviour observed repeatedly under real failure ✓ · first cross-repo bug found BY an agent run and fixed (Entain json-array params, `sportsdata-mcp` v0.2.3) ✓.
  **One explicit carve-out:** the literal one-shot "best price + value" bookmaker demo converges only partially — AU books expose no narrow priced routes (Sportsbet Markets ~1.8 MB, PointsBet featured ~1.1 MB; all bust any sane context cap), so runs hit the cost ceiling navigating. The data IS there (verified by hand: Bulldogs 1.72 / Crows 2.13 on Sportsbet) — making it agent-affordable is **exactly M2.1's ingestion worker + MCP caching** (the plan's phasing predicted this). Interim: `book_navigation` skill ships verified entry points (Sportsbet 4165, PointsBet 7523, TAB names) + size-block guidance.

---

## Phase P1 — Track & converse: Slack, performance, first sandbox

**Goal:** log a user's bets, report ROI/CLV in Slack; one analysis runs in a sandbox.

### M1.1 — Gateway service ✅
- [x] `gateway/app.py` (FastAPI): `POST /message` (sync), `?mode=async` → task id, `GET /tasks/{id}` + `GET /tasks/{id}/events` (SSE progress via a QueueRecorder mirroring the run's recorder hooks), `/conversations/{id}/message` (channel threads), `/agents`, `/healthz`. No-op auth dependency resolving tenant/workspace from headers (§12 seam); per-tenant in-memory rate limiter; one warm `TeamSession` per process; audit rides the existing DbRecorder. `agents serve` CLI.
- [x] Task queue: **in-process asyncio `TaskStore`** (submit→id→poll/stream; error surfacing; eviction). *Deviation, recorded: Redis/Arq is a deploy concern (P4) — the TaskStore interface is the seam.*
- [x] **Exit gate:** test client drives sync/async/SSE/404/conversation routes + rate limiter + task-error surfacing (10 offline tests); live run: healthz → sync answer → async task with SSE events → done status.

### M1.2 — Slack adapter (`D4`) ✅ (live-verified 2026-06-11)
- [x] `interfaces/slack/app.py` (Bolt, **Socket Mode** — no public URL): @mention + DM + `/ask` → gateway `/conversations/{thread}/message` (Slack thread = conversation key) → threaded reply with sources, grounded/unverified badge and the §14 disclaimer; `push_notification()` for agent alerts (graceful when unconfigured); `agents slack` CLI. *(OAuth multi-workspace install = P4 SaaS concern.)*
- [x] **Exit gate:** adapter logic tested offline (6 tests) **and live**: real threaded answer + push alert delivered into #all-daniel via bot `sportsagent` (tokens in `.env`). Interactive @mention loop = `agents serve` + `agents slack`.

### M1.3 — Sandbox integration (`D5`, `§10`) ✅
- [x] `sandboxes/base.py`: `Sandbox` protocol `run(code, files, env, network_policy, timeout)` → `SandboxResult` (stdout/stderr/artifacts). **LocalSubprocessSandbox**: temp-dir isolation, CPU+memory rlimits, wall-clock cap, output caps, path-escape guard, artifact collection. *Documented caveat: egress is advisory locally (macOS can't syscall-block without root).*
- [x] **E2BSandbox** (`e2b.py`): per-run microVM, per-run env secrets, ENFORCED egress allow-list — test-driven; live needs `E2B_API_KEY` (factory auto-selects it when keyed).
- [x] `run_python` native tool (artifacts saved under ./artifacts/), **gated**: only specs with `sandbox: ephemeral` may carry it (runtime build refuses otherwise).
- [x] **Exit gate:** pandas computation runs in the sandbox with verified output (real subprocess test); failure/timeout reported not raised; file round-trip; escape rejected; gating tested (8 tests).

### M1.4 — Reporting / tracking agents (`§6` Tier 3, advisory-only) ✅
- [x] `tools/tracking.py` (session-bound, DB-backed via the new `extra_tools` seam through Runtime/open_team/TeamSession): `log_bet` (journals what the USER placed), `settle_bet` (P&L + closing_odds → **CLV**), `list_bets`, `performance_report` (ROI/P&L/hit-rate/avg-CLV; persists a **`performance`** row — the table M0.3 deferred here, model + guarded migration 0003), `exposure_check` (the risk gate: caps any single recommendation at cap% of bankroll given open exposure). DB-less teams still open: known session tools degrade to an actionable stub.
- [x] Specs: `bet_tracker`, `bankroll_manager` (half-Kelly default + exposure gate), `bet_notifier` (zero tools, banned-language rules, forbidden_capabilities), `concierge` (plain-language explainer). Orchestrator delegates += tracker/bankroll.
- [x] **Exit gate:** log 3 → settle (win/loss/void with closing odds) → report: ROI 11%, avg CLV ≈0.06%, hit-rate, persisted performance row — exact-value test; double-settle guarded; exposure gate caps 80→50 on a 1000 bankroll. *Slack delivery of the report rides M1.2's `push_notification` — live pending your Slack tokens.*

### M1.5 — Memory service (`§8.2`) ✅
- [x] `tools/memory.py`: `remember` (upsert fact/preference/note, tenant-scoped, `memory` table) + `recall` (keyword v1 over key+value; **pgvector semantic recall (D11) deliberately deferred** behind the same tool signature). Granted to the orchestrator; session-bound like tracking.
- [x] **Exit gate:** preference remembered in one session recalled by a NEW session; notes persist (DB, not window — survives any context reset); upsert replaces not duplicates; tenant isolation proven.

### M1.6 — Data-analysis agent ✅
- [x] `specs/data_analysis.yaml`: `sandbox: ephemeral`, `run_python` + stats capabilities + `lookup_book_ids`, typed `StatsAnswer`, plt.savefig discipline in the prompt. Orchestrator delegates += data_analysis.
- [x] **Exit gate (machinery, deterministic):** a scripted run computes form over 10 games in the REAL sandbox, saves a chart artifact, and the typed answer quotes only computed numbers — `verified=True` because the grounding check matches the answer's 98.0 against run_python stdout. *(LLM-quality grading of live chart requests belongs to the M2.4 eval harness.)*

- [x] **🚪 P1 EXIT GATE — CLOSED** (2026-06-11): **Slack live** ✓ — the real adapter flow (handle_question → gateway → model → threaded reply with grounded badge + §14 disclaimer) posted into #all-daniel, and a push alert delivered (`push_notification` → 🔔). Bet tracking + CLV ✓ (exact-value tests; `performance` table live). Sandboxed analysis ✓ (real pandas run; chart artifact; grounding verified the quoted number). Advisory invariants ✓ (no placement tools exist anywhere; tool-less notifier with banned-language rules; exposure gate caps stakes; deny-filter enforced at authoring + runtime). Gateway live ✓ (sync + async + SSE, verified answers). *Interactive @mention loop: run `agents serve` + `agents slack` (Socket Mode).*

- [x] **P1 review fixes** (2026-06-10, full M1.1–M1.6 code review): hit-rate now counts **decided** bets only (voids excluded — was diluting the headline stat); `settle_bet` persists the outcome as status (`open → won|lost|void`) instead of flattening to "settled"; `exposure_check` actually enforces open exposure (single cap **and** `total_cap_pct` ceiling, default 25%); gateway async runs pass a **per-run recorder** (contextvar in the harness — the old harness-mutation raced under concurrency; regression-tested with two simultaneous runs); SSE late-join terminates instead of hanging; TaskStore evicts oldest-finished only + awaits cancellation on close; healthz 503s before ready; Slack DM handler ignores subtype events (edits/deletes) and `/ask` posts unthreaded (Slack rejects `thread_ts=""`); local sandbox CPU rlimit follows the caller's timeout (was pinned 60s), collects subdirectory artifacts, `run_python` takes `timeout_s` (≤300); the local sandbox docstring now states the filesystem-read + advisory-egress exfiltration risk bluntly (E2B before P2 ingestion); `performance` row upserts (one all-time row, not one per call); memory gains `forget` + a unique `(tenant, workspace, key)` constraint (migration 0004, dedupes first); Postgres CI job added (see Testing). **Known deviations carried to P2:** `conversation_id` accepted but turns stay independent (threading = P2 backlog below); chart artifacts stay on the server's disk — Slack delivery needs `files_upload_v2` (P2 backlog).
  **Review pass 2** (same day, deep internals — harness loop/grounding/gateway/compactor/pool all read clean): grounding's verbatim fallback is now boundary-guarded (bare substring let a fabricated "42" verify against "15423" in any id — the §13.1 badge erred toward false-grounded); per-model-call timeout is `min(120s, workspace budget)` so one wedged call can't eat the run deadline (fallback/retry keep headroom); §8.1 spec-limit clamping logs what it clamped (a 600s spec on a 300s workspace silently ran at 300).

---

## Phase P2 — Quant: models, value, backtests, ingestion

**Goal:** a model backtests with CLV > 0 on held-out data; value alerts fire.

### M2.1 — Ingestion worker + odds-history warehouse (`§9.1`/`D25`) ✅
- [x] `operations/ingestion/`: `Feed` registry (tool + mcp_groups + normalizer + interval) → `ingest_once`/`run_loop` write `odds_snapshots` (raw, prunable) and `prices` (change-points — the dedupe IS the series) + `event_results` (M2.3 settles against it). Migration 0005; **Timescale attempted, not required** (*deviation, recorded: hypertable + 90-day retention DDL applied when the extension exists; plain Postgres/SQLite get ordinary tables + `prune_snapshots` — local Docker is down and CI postgres:16 carries no Timescale, so the guarded path is what's exercised*). Composite PKs include the time column so hypertabling stays possible.
- [x] Per-feed schedules (`run_loop` with injectable clock), per-feed failure isolation (one bad feed logs, the rest ingest), dedupe to change-points, retention via Timescale policy or `prune_snapshots`. Shipped feed: `nba_odds` (CDN, group `nba.public.cdn`); a provider = one normalizer + one registry row. `agents ingest --once/--loop [--prune N]`, `agents movement <event>`.
- [x] **Exit gate:** offline — 3 captures/1 move → 6 snapshots, 3 change-points, movement query ordered with prev→new (7 tests). **Live** (2026-06-10, SQLite warehouse): two real captures 45s apart → 44 first-sighting change-points then 44 snapshots / **0 changes** (dedupe proven on real data); `agents movement 0042500403 --selection home` renders the 5-book series.

### M2.2 — Modelling agent ✅
- [x] `specs/modelling.yaml` (sandbox: ephemeral + warehouse access via `query_line_movement`) — `quant/metrics.py` (Brier/log-loss, ONE definition shared with M2.4 eval), `calibration_metrics` native tool, session-bound `tools/quant.py` (`save_model` **refuses uncalibrated models**, `record_predictions` prob-validated + tenant-scoped, `list_models`); `models`/`predictions` tables (migration 0006); orchestrator delegates += modelling.
- [x] Skill bundles: `build_a_totals_model` (normal-approximation baseline, holdout discipline, "Brier ≥ 0.25 = say so plainly"), `calibrate_probabilities` (shrinkage/Platt rescaling, before/after reporting).
- [x] **Exit gate:** deterministic machinery run — run_python computes holdout probs in the REAL sandbox → calibration_metrics (Brier 0.19 exact) → save_model persists v1 WITH the calibration record → 2 predictions recorded → typed answer grounding-verified. Version increments, cross-tenant prediction writes refused (9 tests).

### M2.3 — Value-finder + backtesting ✅
- [x] **Value-finder** — `quant/value.py` (vig-removed fair probs, EV/edge %, fair odds; full-market validation) behind the `value_finder` native tool; `specs/value_scout.yaml` (no saved model = no improvised probs; steam/drift honesty via `query_line_movement`).
- [x] **Backtester** — `quant/backtest.py` replays predictions vs the `prices` change-points + `event_results`: flat-stake edge-threshold strategy → ROI/hit-rate/**CLV** (entry vs close)/P&L variance, skip accounting (no_price/no_result/below_edge); `run_backtest` + `record_result` session tools; `specs/backtester.yaml` ("lead with CLV; 3 bets is an anecdote — say so"). Orchestrator delegates += value_scout, backtester.
- [x] **Exit gate:** seeded price history + results, held-out predictions, edge≥5% strategy → 2 qualifying bets, ROI +5%, **avg CLV +8.20% > 0**, variance 1.1025, skips {1,1,1} — exact-value test; value alert computed (edge 7.3% on home @1.85 with p=0.58 flagged, sub-threshold not).

### M2.4 — Eval harness (`§16.3`) ✅
- [x] `evals/` runner: **offline deterministic** scores from committed goldens — calibration (1−Brier over golden holdout), **CLV** (real backtest replay over a golden in-memory warehouse — the gold metric), grounding (8 verifier cases incl. digit-soup false-positive and fabrication-tolerance pins). Every score higher-is-better; one gate rule (`baseline − tolerance`), and a silently DROPPED eval trips the gate too. `agents eval [--baseline|--write-baseline]`, baseline committed.
- [x] **Live evals** (`-m eval`, key-gated): routing efficiency (a stats question must delegate — the orchestrator answering alone is fabrication by construction) + value_scout answer accuracy (quotes value_finder's edge, grounding-verified) joining the M0 golden Q&A. Deterministic source-match = the grounding verifier; LLM-judge deferred until a case needs it (*deviation, recorded: scores-table CLI stands in for dashboards until the admin UI*).
- [x] **Exit gate:** `eval.yml` scheduled workflow (Mondays + manual dispatch; offline gate always, live evals only when a key secret exists — absent key = honest skip). Suite produces scores (0.7907/0.5820/1.0000), gate passes on baseline, and a deliberately-worse change (miscalibrated model / deleted eval) FAILS — unit-tested. Live: all 4 `-m eval` cases pass with the real model + MCP (49.9s).

### P2 backlog (carried from the P1 review) ✅ (done 2026-06-10)
- [x] **Conversation threading**: `gateway/conversations.py` — turns persist to `conversations`/`messages`, the most recent turns prefix the next prompt (`threaded_prompt`), storage failures degrade to a stateless turn. Explicit microsecond timestamps (server defaults are second-resolution; random UUIDs would shuffle same-second turns). Tenant-scoped; DB-less deployments stay stateless.
- [x] **Artifact delivery to Slack**: the harness harvests `artifacts` paths from tool payloads onto `RunResult` → `MessageOut.artifacts` → the adapter uploads via `files_upload_v2` into the thread (missing files skip; upload failure never breaks the answer).
- [x] **Sandbox isolation flag**: `SPORTSDATA_AGENTS_REQUIRE_SANDBOX_ISOLATION=1` makes `get_sandbox()` refuse the local fallback (set it when untrusted text flows into prompts); without it the local backend warns once, loudly, about its limits.

### P2 review (2026-06-10) — fixed same day
- Backtest **lookahead bias**: entry was the first-ever captured price even for later predictions; entry is now the prevailing change-point at `predicted_at` (`record_predictions` accepts backdated timestamps for historical replays; a late prediction enters at the close and earns zero CLV — regression-tested).
- `record_result` upserts (corrections, not duplicates); the result lookup survives legacy dupes. Ingest reports empty feeds visibly. Modelling skills generalised (see below).

### P2 comprehensive review fix pass (2026-06-11) — B1–B12 + futures, fixed same day
- **B1 dialect safety**: `list_market_names` used SQLite-only `group_concat` — provider lists now aggregate in Python from a plain grouped query (exercised by a test that runs under the Postgres CI job too).
- **B2 rotation across processes**: `_take_rotating` offsets were process-lifetime, so cron `--once` runs re-fetched the same first window forever — windows now derive from wall clock (`ROTATION_EPOCH_S=600`): restart-safe, advances with time.
- **B3 real start times**: `odds_snapshots.start_time` column (migration 0007, guarded) parsed at write time from provider meta (`start_time`/`post_time`; ISO or epoch); the resolver windows fixtures on the ADVERTISED start, falling back to first-capture day — futures captured weeks apart by different books now share a day window (regression-tested) and fixtures carry real future dates (live: 327 fixtures dated beyond +9 days).
- **B4 resolution-aware settlement**: `run_backtest` settles through the fixtures join — a result recorded under any book's event id settles every book's series; side-relative winners ("home"/"away") translate between books' listing orders by name-token matching and stay UNSETTLED when orientation can't be established (a flipped side corrupts ROI silently). New session tools `find_fixture` + `best_prices` expose the cross-book board to agents (granted to value_scout). Settlement maps load once per backtest (the per-prediction result query is gone).
- **B5 write-path N+1**: `record_points` issued one latest-price SELECT per point (66K/cycle) — now a handful of grouped queries per batch (latest odds per key, chunked by event id).
- **B6 PointsBet double fetch**: the ~5MB event details were fetched by BOTH `pointsbet_all` and `pointsbet_books` — the hot feed is listings-only now (its inline insight/featured markets capture generically), cadence 1800→900s.
- **B7 steward guard blind spot**: the qualifier guard only protected BASE families — live evidence: the steward merged "spread p1 alt" into "spread_alt". The rule is now general (the alias's qualifier tokens must appear in the family name); the live override file was corrected ("spread p1 alt" → own family).
- **B8 FanDuel discovery**: the hardcoded six-page list is gone — `application_context` nav links (`/navigation/{slug}`) ARE the content-page ids; live discovery added nfl, ncaaf, ncaab, ncaaw, pga, esports, fifa-world-cup and more (fallback list retained for outages). Pages now stamp their slug as the sport label (was "?").
- **B9 Kambi outrights**: `matches.json` excludes competition events — new `sport_competitions` op in the data plane (`/listView/{sport}/all/all/all/competitions.json`, probed live: same `{events:[{event,betOffers}]}` shape, zero normalizer change); `fetch_unibet_all` walks both per sport. Live: 4,525 winner-market snapshots in one cycle (premiership/conference/super-bowl winners).
- **B10 Sportsbet outrights**: futures competitions (Brownlow, NFL Futures…) list no match-type events — `fetch_sportsbet_all` now calls `competition_outrights` alongside `competition_matches` (same grouped shape, same normalizer; verified live on the Brownlow board).
- **B11 racing futures tier**: 4 new feeds @60min — **TAB** (futures meetings → cards via new `tab_racing_futures_race` MCP op: futures URLs put the race NAME in the race-number slot; runners are unnumbered → horse-name selections, race-name-keyed events), **Sportsbet** (Futures listing is sportsbook-shaped; `event_markets` prices them), **PointsBet** (racing-futures listing → standard event details), **Unibet** (`FuturesQuery` → `EventQuery`; ante-post prices carry no flucs → direct price fallback). Live first cycle: TAB 1,486 / SB 817 / UB 1,104 / PB 19 points. **Entain racing futures blocked** (same persisted-hash drift as the race card; Entain sports outrights flow via REST regardless).
- **B12 futures starvation + naming**: `pointsbet_books` reserves rotation slots for the furthest-out events (soonest-first starved outrights); Pinnacle outright matchups are named from the special description / league (live: zero new "? v ?" names — "2026 Grey Cup Winner" etc.).
- **Resolver one-name gate** (found in live verify): plain Jaccard merged "Argentina Markets 2026" with "Brazil Markets 2026" (generic tokens dominate) — one-name events now gate on the same fuzzy-subset rule as two-sided events, Jaccard only ranks (threshold 0.4 floor). Live over-merge purged and re-resolved into 56 separate fixtures; "Queen Anne Stakes" still joins its longer TAB futures name. *Known limit, noted:* outright names containing " - " (Sportsbet racing futures) mis-split as two-sided and stay book-local.
- README rewritten for the current state (was stale since v0.2.0). MCP repo: 2 stale AFL tests fixed (statspro count 9→11; fastmcp ToolError wrapping). Gates: ruff+mypy+344 offline tests green, eval gate green (golden numbers unchanged), both repos. **Registry: 23 feeds (9 hot + 5 books + 5 racing + 4 racing-futures).**

### Second review pass (2026-06-11, same day) — three fixes + three additions
- **Predictions own their frame**: `record_predictions` now REFUSES home/away/draw selections without a `provider` — a side is meaningless without knowing whose listing order it refers to (golden + tests updated; non-side selections stay optional).
- **Far-future day windows**: books placeholder outright dates and disagree by days-to-weeks — events >30d out window at ±14d (the fuzzy-subset name gate still decides; regression-tested with a 10-day spread).
- **Entain categories discovered**: `SportingCategories` GraphQL op (verified live, UUIDs match the doc snapshot) replaces the hardcoded map, which remains as the gateway-outage fallback. Feed gains the `entain.graphql` group.
- **League results via scoreboards (the missing settlement leg)**: `agents results` settles racing placings AND league finals — NBA live scoreboard (gameStatus 3), AFL matches list (CONCLUDED, totalScore), NRL fixture (matchStatus complete; competitions discovered by name/season from the Champion Data catalogue). Winners record as home/away/draw in the SCOREBOARD's frame with the event name in meta; `map_events_to_fixtures` joins them onto existing fixtures (never founding one — a result no book priced settles nothing); backtest translation reads the result's meta name (scoreboards have no snapshots). Live: 34 AFL + 112 NRL + 893 racing results recorded; fixture mapping verified by test (live games all predate the warehouse's June-10 start — tonight's fixtures map as they conclude). Cron daily.
- **Steward cadence**: `agents steward` runs the market_steward's standing audit (cron weekly); `agents dictionary-promote [--write]` merges curated overrides into the committed seed and clears them.
- **CLV benchmark + resolver eval**: `run_backtest(clv_book="Pinnacle")` benchmarks CLV against the sharp book's close at the same fixture (selection orientation-translated; per-bet fallback to own close; `clv_benchmarked_bets` reported). The offline eval gate gains a `resolution` golden (4-book join + ambiguity skip, score 1.0 baselined) — the resolver can no longer regress silently. Gates: 349 offline tests, eval gate green.
- **Final P2 review (third pass) fixes**: result upserts and the backtest's direct settlement lookup key on **(provider, event id)** — five result providers share one numeric id namespace, and a collision now poisons only the ext-only fallback (settlement falls through to the fixture join) instead of overwriting or mis-settling; `record_result` gains an optional `event_name` (into meta) so agent-journaled home/away results can orientation-translate across books like scoreboard results do; AFL results paginate (a busy AFL+AFLW+state-league week overflows one page of 50). 350 offline tests.
- **All team sports settle — first-party else ESPN** (Daniel's directive): `_mlb_results` (MLB StatsAPI, codedGameState F) joins NBA/AFL/NRL as first-party; everything else reverts to `_espn_results` — one generic `espn_scoreboard` collector over a catalogued league list (NFL, college football/basketball ×2, NHL, WNBA, college baseball, EPL/MLS/UCL/World Cup/A-League; extend by adding rows; the aggregator exclusion was about ODDS — results are facts). Live: MLB 19 finals + ESPN 4 (WNBA/NHL — the leagues in season in June; the rest activate as their seasons start). **Local cron installed** (the interim ops loop until P3's daemon), 5 marker-tagged lines (`# sportsdata-agents-cron`) in Daniel's crontab: ingest every 3min (`--cron 180` stateless pacing), daily 23:30 resolve+results, weekly Mon 09:00 steward, weekly Sun 06:00 refresh-books; logs in /tmp/agents-*.log. The whole capture→resolve→settle→audit loop now runs hands-off.

### Quant ideas backlog (from the P2 review — future milestones)
- [ ] **market_monitor agent** (M3.x ops): scheduled value scans (model predictions × fresh prices → `value_finder`) pushing alerts via `push_notification` — "value alerts fire" becomes push, not pull.
- [ ] **ratings_keeper**: maintain per-team Elo/power ratings as a persistent feature store the modelling agent reads (P8: deterministic updates, LLM only narrates).
- [x] **Books of record only** (2026-06-11): the NBA CDN aggregator feed is OUT of the registry (second-hand affiliate prices) — replaced by direct NBA h2h feeds from Sportsbet (6927), TAB (Basketball/NBA), PointsBet (7176), Pinnacle (487, **+ spread/total lines** — zero extra calls, the markets were already fetched), Unibet (basketball + `only_group="NBA"`), Entain (Basketball UUID + `only_competition="NBA"`), BetR (39251), and **FanDuel US sportsbook** (content-page → event-page fetcher; MONEY_LINE runners carry decimal odds + HOME/AWAY tags). **FanDuel Racing live** (`fanduel_racing_win`, 120s cadence: featured races → race cards; tvgRaceId-keyed, win market, saddle-number selections, scratched runners skipped). Live: 22/22 feeds, finals game captured from 6 books directly, 65 racing runner prices. **Registry: 22 feeds, 9 providers, 5 sports, 4 market types.**
- [x] **RESOLUTION MILESTONE — DONE 2026-06-11** (event dictionary + market dictionary-as-data + first results leg). Original framing: wholesale stats-site ingestion is NOT planned — the data plane already serves every stats endpoint on demand and facts don't decay like prices; duplicating it into the warehouse buys a sync problem, not signal. What the quant loop needs PERSISTED from stats sites is **results** (settlement), and results only matter when they JOIN book events — so event resolution (fixtures/events mapping by team-name + start-time across each book's ids) and league results feeds (scoreboards → `event_results`) land together. After that: per-model feature caches (boxscores etc.) as models demand them, not wholesale.
  **Delivered:** (1) **Market dictionary as DATA** — packaged `market_dictionary.json` seed (markets + sports sections) + local overrides file; `canonical_market`/`canonical_sport` load from it (zero hardcoding; unmapped names flow book-named). (2) **market_steward agent** — audits the warehouse's market names and maintains the dictionary; merge safety enforced IN THE TOOLS (qualifier names can never alias into base families); live run mapped 14 names (~2,740 rows) with rationales and refused 7 genuinely ambiguous ones with reasons. (3) **Event resolution** — deterministic resolver (`agents resolve`): per-side fuzzy-subset token matching (abbreviation-tolerant: "Wst"≈"Western"; nickname-strict: Swans≠Roosters; swap- and @-tolerant), ±1-day windows, ambiguity SKIPPED never guessed. Live: 3,080 book events → 3,062 mapped onto 2,121 fixtures, 16 ambiguous, 125 fixtures joined by ≥3 books — **Bulldogs v Adelaide joined across 7 books and `cross_book_prices` ranks best-first** (away: Pinnacle/Sportsbet 2.12 … Unibet 2.06). (4) **Racing results** — PB placings (zero extra calls) → `event_results`: **869 races settled live**. Remaining legs: league-sport results via scoreboards (fixtures now exist to join them), market-dictionary review cadence (steward weekly).
- [x] **Discovery-driven all-sports coverage** (2026-06-11): per-competition rows replaced by **one self-discovering feed per provider** — each walks the book's own discovery route every cycle, so coverage tracks whatever the book prices, not a curated id list. Sportsbet (nav hierarchy → rotating window of competitions; the dated classes route 400s upstream), TAB (sports tree → rotating MB-scale competition pages, 900s), Unibet (group.json → one listView per Kambi sport, **+ Line/Totals with their lines**), Entain (one bulk call per documented sport-category UUID, novelty/politics excluded), Pinnacle (all active sports → soonest 40 matchups detailed board-wide; h2h+spread+total), PointsBet (full catalogue → soonest event details; "Moneyline" IS "Match Result" for US sports, 1800s), BetR (one priced master-category call per event type), FanDuel US (six sport pages → event pages). Rotation state is process-lifetime (`_take_rotating`) — full-board refresh amortises across cycles by design. **Live: 9/9 providers, 34 sports, 4 market types, 5,841 snapshots in one cycle** (Unibet 2,542 / Entain 1,128 / BetR 472 / Sportsbet 385). Sport labels are each book's own slug — canonical cross-book sport ids land with event resolution.
- [x] **ALL markets, capture-everything policy** (2026-06-11, Daniel's directive: "never leave any out — no code fix when a new market/sport/comp is added"): no normalizer filters markets by name anymore — every market a payload carries is captured under the book's own naming (props included; the cardinality cost — ~50-100x rows vs h2h-only — was accepted). The ONLY mapping layer is `canonical_market()`: renames the h2h/spread/total families onto shared keys for cross-book math, passes everything else through untouched (normalization, never exclusion; Pinnacle periods/alternates suffix `p1`/`alt`; Kambi non-base criteria suffix the label). **Two-tier cadence**: hot tier (5-30min) keeps primary/inline markets fresh; **full-book tier (60min)** pulls each fixture's COMPLETE book — `sportsbet_books` (~2.5MB/293 markets per event), `tab_books` (`tab_match`, ~0.8MB/238), `unibet_books` (`event_betoffer`, ~0.6MB/512 offers), `pinnacle_books` (full-board rotation, 120 matchups/cycle), `pointsbet_books` (full-board rotation, ~5MB details). Entain/BetR/FanDuel/racing hot tiers already ARE their full books. Registry: 14 feeds (9 hot + 5 books).
- [x] **Racing for the AU books** *(done 2026-06-11 — see "AU-book racing feeds" above: TAB/Sportsbet/BetR/PointsBet/Unibet live; Entain racing blocked upstream)*.
- [ ] **(P3) Scheduled ingestion deployment** — INTERIM COVERED (2026-06-11) by stateless cron: `agents ingest --once --cron 180` every 3 minutes (the `--cron N` flag runs only feeds whose interval boundary was crossed in the last N seconds — racing every tick, hot tier at its own 300-900s cadences, books/futures tiers only on hour boundaries; no daemon, no state file). P3 proper still owes: the ops/triage agents watching feed failures, `market_monitor` push alerts, and the Postgres/Timescale move.
  **⚠️ ADDRESS BEFORE RELYING ON THE DATA: the warehouse lives in /tmp** (`/tmp/agents-warehouse.db`, plus `/tmp/market_dictionary.local.json` and all cron logs) — **macOS wipes /tmp on reboot**, so a restart loses every captured price, fixture, result and steward decision. Deliberately deferred (Daniel, 2026-06-11) to be solved properly by the P3 Postgres/Timescale migration rather than moved twice — but a reboot before P3 means starting the series from zero.
- [ ] **(P4) Betfair exchange** — fetcher+normalizer ready; blocked on an authenticated Exchange API key (public readonly key returns no price sections from AU).
- [ ] **(P4) Feature stores + injury/news scout** — per-model feature caches (boxscores, ratings) pulled as models demand them; news/injury ingestion only after §13 prompt-injection guardrails (untrusted third-party text).
- [ ] **(MCP spec gap, noted)**: BetR exposes no per-event full-book route in the data plane — its category call is what there is; add a spec route upstream if BetR depth matters.
- [x] **More odds feeds** *(EVERY viable provider done 2026-06-11)*: **14 feeds across 8 providers** — NBA CDN (multi-book), Sportsbet + TAB + Unibet/Kambi + BetR + Entain(Ladbrokes) + **Pinnacle** (the sharp CLV benchmark; American→decimal) + PointsBet, each AFL+NRL where carried. One normalizer per provider (shapes captured live: Sportsbet top-level list/resultType; TAB propositions/position; Kambi milli-odds + `FULL_TIME_OVERTIME` for NRL; BetR flat outcome rows; Entain UUID-joined maps with fractional odds and " vs " names; Pinnacle matchups+markets via a **multi-call fetcher** (`Feed.fetch` seam); PointsBet ~5MB per-event details → 900s cadence). Ingest subprocess lifts the MCP byte cap to 8MB (`INGEST_MAX_BYTES` — the 150KB default guards model contexts; ETL has none). **Honestly skipped:** Betfair — the public readonly key returns no `RUNNER_EXCHANGE_PRICES_BEST` sections from AU even on a $26K-matched market (verified live); fetcher+normalizer are ready for an authenticated Exchange key (P4). FanDuel US sportsbook adds nothing the NBA CDN feed doesn't carry. Live: one `agents ingest --once` → 13/13 ✓, warehouse census 13 books × 3 sports, 604 snapshots. Cross-book event RESOLUTION (same match, different ids per book) is the next warehouse problem (fixtures/events tables).
- [ ] **injury/news scout** (P4, after §13 prompt-injection guardrails — untrusted third-party text).
- [ ] **Calibration-curve artifact**: modelling agent saves a reliability diagram PNG with each model version (artifacts now reach Slack).

- [x] **🚪 P2 EXIT GATE — CLOSED** (2026-06-10): end-to-end quant loop green on one warehouse, every stage the real implementation (`test_p2_exit_gate.py`): two ingest captures (entry 2.10 → close 1.90) → model calibrated on holdout (Brier 0.19) + persisted + predictions recorded → value_finder flags home at **+26% edge** (the computed alert) → result settles, backtest replays: 1 qualifying bet, P&L +1.10, **avg CLV +10.53% > 0**, below-edge skip counted → offline eval gate green on baseline. Live legs verified separately: real NBA feed captured twice with dedupe (M2.1), all 4 `-m eval` cases with the real model (M2.4). **Quant caveat, stated plainly:** the exit gates prove the MACHINERY (capture→calibrate→edge→CLV→gate); profitable models are an ongoing practice, not a milestone — golden datasets pin the math, the weekly eval gate watches for regressions.

---

## Phase P3 — Self-maintaining + alerts + fantasy + GTM

**Goal:** ops agents maintain the repos; alerts fire; fantasy works; the public demo is live.

### M3.1 — Operations plane (`§3.1`, platform-only) ✅ (2026-06-11)
- [x] **Hard plane split in INFRASTRUCTURE**: `AgentSpec.plane` (product|ops); the customer gateway REFUSES ops agents and filters them from team mode; lint refuses product→ops delegation; only `agents ops run` injects platform tools. Platform creds (OPS_GITHUB_TOKEN / credential helper) resolve lazily inside ops tools only.
- [x] **MCP health/QA agent** (`mcp_health`): run_doctor + run_contract_suite + feed_health; files deduped GitHub issues on real breaks. `agents ops health` = the deterministic no-LLM shortcut (live: caught pointsbet_racing silent past 3x cadence).
- [x] **Repo-improver** (`repo_improver`): list/read repo files + `propose_change` (NEW branch only — refuses main; repo-confined paths; surgical find/replace edits after whole-file rewrites blew the token budget live). **Live: opened PR #1** (stale CLI docstring), CI green.
- [x] **Code-reviewer** (`code_reviewer`): diff-driven gh_review_pr (approve/request_changes/comment); **no merge tool exists — structurally**. Live: reviewed PR #1; GitHub refused self-approve (same account) → comment review; production needs a separate bot account (P4 note).
- [x] **Eval/benchmark agent** (`eval_benchmark`): run_offline_evals (gated vs baseline) + record_agent_metrics rows.
- [x] **Incident-triage agent** (`incident_triage`): feed_health → remediate_feed within the CLOSED allow-list (retry/disable/enable — durable ops state the ingest CLI honours) else escalate (ops-state entry + Slack).
- [x] Aggregated signals only: feed_health/evals expose counts and scores, never raw rows.
- [x] **Exit gate ✅ (live)**: health caught a silent feed; improver opened CI-passing PR #1; reviewer reviewed it; the merge awaits a human (Daniel).

### M3.2 — Line-monitor / alerting ✅ (2026-06-11)
- [x] Standing watches: line_move / steam / value appear+vanish / scratching-suspect over the price stream; durable per-watch cursors (missed cycles replay); per-condition dedupe + max_alerts_per_cycle cap (live lesson: the first unbounded pass over a 6h backlog got Slack rate-limited). Push: Slack chat.postMessage per subscription channel; push failure never sinks the watch.
- [x] `subscriptions` + `alerts` tables (migration 0008); create/list/delete_watch + list_alerts session tools (value_scout); `agents monitor [--add name:kind:threshold]`; cron 5min.
- [x] **Exit gate ✅ (live)**: a 10% line_move watch fired 10 alerts on real racing moves, pushed to Slack (#all-daniel), pushed=true rows in `alerts`.

### M3.3 — Fantasy advisor + agent-builder + Discord ✅ (2026-06-11)
- [x] **Fantasy advisor**: `optimize_lineup` deterministic beam-search optimiser (exhaustive-oracle tested; multi-position, locks/exclusions, G/F/UTIL families) + dfs_lineup_building skill + sandboxed run_python for projection math.
- [x] **Agent-builder** (§7.1): list_capabilities → draft_agent_spec (validated against the REAL spec models + lint at draft time) → save_agent_spec (user specs dir; version bumps archive `{id}@{version}.yaml` per D27). Guardrails by construction: product-plane only, no builtin-id collisions, no-money invariant. User specs merge into TeamSession (builtins never shadowed — a parameter-shadowing bug here was caught by the ops-plane test the moment the live run populated the dir).
- [x] **Capability→friendly-label map**: capability_labels.json — 52 tags generated from the MCP catalogue with hand-curated headline labels ("Live odds", "Race cards & runners").
- [x] **Discord adapter** mirroring the Slack shape (mention/DM → gateway); routing core unit-tested without discord.py; optional `[discord]` extra; `agents discord`.
- [x] **Exit gate ✅ (live)**: fantasy optimised a 3-slot cash lineup from user projections ($0.11); "build me an nrl_form_guide agent" → saved v0.1.0 ($0.06) → loads and runs with real MCP calls (capability picks are an iteration item — it chose generic tags over NRL-specific routes).

### M3.4 — Marketing site + live MCP demo (`§11.1`) ✅ built 2026-06-11 (public deploy = operator hosting decision)
- [x] Static site (`site/`, framework-free first cut — D21 allows Astro later): hero, live capability counters (`/demo/stats`), the D22 hybrid demo (curated prompt chips → POST `/demo/run` with tool calls animated; recorded-playback fallback from demo-fallback.json when the gateway is offline), persona cards, lead form → `/leads` (DB row, file fallback — a lead is never lost; `leads` table in migration 0008).
- [x] Demo abuse posture BY CONSTRUCTION: free-form input does not exist (curated prompt ids only), fresh budget-capped TEAM session per run ($0.30, tight limits), per-IP rate limit (3/min), tool trace carries names+timings only — never arguments/payloads/secrets.
- [x] Hosted/remote-MCP channel (D23): docs/hosted-mcp.md — stdio config for any MCP client today (scoped via SPORTSDATA_MCP_GROUPS), proxy recipe for remote; productised auth/metering is P4 billing work.
- [x] **Exit gate (local) ✅ (live)**: `/demo/run nba-finals` → 4 real tool calls shown → complete grounded answer (caught Game 4 IN PROGRESS), $0.06; rate-limit + curated-only + no-secret-trace covered by tests. *Public deployment*: drop `site/` on Vercel/Netlify + point GATEWAY_URL at a hosted gateway — Daniel's hosting call.

### M3.5 — Spec/module versioning (`§7`/`D27`) ✅ (2026-06-11)
- [x] Semver per spec (already enforced) + `{id}@{version}.yaml` archives (load_spec_catalog; filename must match contents); `Workspace.agent_versions` pins resolve in TeamSession (unknown pin fails loudly); `deprecated` notice loads-with-warning so pinned workspaces never break; spec_version schema guard refuses future schemas with a clear message. The agent-builder's save flow archives automatically.
- [x] **Exit gate ✅**: test bumps a spec to 0.2.0 with 0.1.0 archived — unpinned workspaces get the new version, a pinned workspace keeps the old, re-pinning IS the opt-in migration.

- [x] **🚪 P3 EXIT GATE — CLOSED 2026-06-11** (one human step pending): the self-improvement loop ran END TO END LIVE — repo_improver opened PR #1 (a real stale-docstring fix), CI went green on the branch, code_reviewer read the diff and submitted its review; **the merge button is Daniel's** (by design — and GitHub's no-self-approve rule means production wants a separate reviewer bot account). Alerts fired to Slack on real line moves; fantasy optimised a live lineup; the demo answered a real bounded query with visible tool calls. Remaining operational: deploy site/ + a hosted gateway publicly (hosting decision), merge PR #1.

### P3+ additions (2026-06-11, post-exit-gate) — prediction markets + GTM polish
- [x] **Final-review hardening (v0.13.0)**: the **data custodian** (`agents custodian`, conductor-run hourly) — adaptive disk-aware retention: plenty of space → hold and wait (weekly gzip backup regardless); tightening → backup → prune `odds_snapshots` on a sliding ladder (60→14d; `prices` change-points NEVER pruned) → VACUUM only with headroom; <10% free escalates to the operator. Deterministic by design — no LLM decides what data dies. **Pace scope fix**: the proximity floor applies only to ≤15min tiers (flooring the 60-min firehoses made one cycle outlast the racing cadence — racing was silent 40min, observed live). **`agents serve --demo-only`**: the only publicly hostable mode until P4 auth (middleware 404s everything but /healthz, /demo/*, /leads). **Value-alert outcomes**: the honesty loop re-probes value edges at +5min (payload enriched with prob/keys); `alert_quality` reports per-kind takeable rates. **Doubleheader guard**: same-day same-teams events with advertised starts >3h apart never merge (far-future placeholders exempt).
- [x] **Review-3 closeout (v0.11.0)**: 42 KX*GAME series seeded onto h2h (NBA through World Cup to esports — kalshi game lines now join cross-book boards and arb scans everywhere; kxwcgoaleverygame deliberately unseeded: a novelty derivative, not a winner line); **alert outcome tracking** (5min after an arb alert the SAME board re-measures, outcome stamped into the payload; `alert_quality` ops tool aggregates takeable-rate + median margin decay; eval_benchmark reports it weekly); arb demo chip ("⚡ Any arbitrage?" — honest zero-arb answer) + platform-tour arbitrage bullet; **arbitrage + scheduler eval goldens** (the gate caught a wrong expected margin in its own golden on first run — working as intended); README Status → P3 complete + conductor docs.
- [x] **The conductor (v0.10.0)**: `agents schedule --cron 60` — ONE cron line replaces the nine per-job lines. Deterministic dispatch (no LLM in the tick): ingest gets **event-proximity pacing** (nearest upcoming fixture within 6h floors the feed cadence: 30→20→15→10→5→3→2min as start approaches — live-verified pace=120 with a fixture 5min out), calendar jobs fire on stateless wall-clock boundary crossings, per-job pid lockfiles stop stacking, outcomes durable in ops_state (`agents schedule --status`). **Failure handoff to the error agent**: 2 consecutive failures → immediate deterministic `ops health`; 3 → the `incident_triage` ops agent diagnoses/remediates within its allow-list, rate-limited to one run per job per 6h. Arb review fixes rode along (odds floor in arbs_for_fixture, soonest-first deterministic fixture cap BEFORE the bulk queries, column-tuple snapshot select, tool param clamps). ⚠️ Crontab swap pending: macOS blocked cron writes mid-session (TCC prompt) — run `sh scripts/install-scheduler-cron.sh` once and approve the dialog; the old 9 lines stay functional until then.
- [x] **Arb hunter (v0.9.0)**: deterministic cross-book arbitrage in `quant/arbitrage.py` — orientation-translated sides, per-book-complete outcome frames (a 2-way board never fakes an arb on a 3-way market), same-line totals, exchange NO-folds on 2-way frames, snapshot-window legs (a delisted market's stale change-point must not arb), PRE-GAME only (one in-play leg fakes monster margins). `find_arbs` session tool; monitor watch kind `arb` (margin-bucketed dedupe so growing arbs re-fire); `arb_hunter` product agent on the orchestrator roster. **Live gates**: watch fires+dedupes offline; live scan 38s over the warehouse; agent run $0.0094; standing watch → Slack C08BEVD0080, monitor cron restored at */5 (line-move watches stay off). **Found live along the way: the resolver merged team VARIANTS (women's/U23/reserves) onto the senior fixture** — "Blues Women v Hurricanes Poua" joined "Hurricanes v Blues" and manufactured a 74% "arb"; `_side_ok` now refuses marker-mismatched sides and 22 live mis-joins were purged + re-resolved. Known limit: same-day MLB doubleheaders can still merge (pitcher-annotated listings mostly self-exclude via the "b" initial marker).
- [x] **Site live on GitHub Pages** (playback mode, public `sportsdata-site` repo; private-repo Pages blocked by plan): full redesign, animated console demo, logo marquees, cancellable playback; republish via `scripts/deploy-site.sh` (noreply identity enforced).
- [x] **Prediction-markets feed tier** (v0.7.0): `kalshi_all` (open events + nested markets, cursor-paged; live: 11,001 snapshots first cycle) and `polymarket_all` (volume-ordered Gamma events; **the Gamma edge geo-blocks AU** — feed degrades gracefully, runs wherever the edge answers). Exchange probabilities captured as decimal odds (1/ask). Market keys are STABLE: Kalshi uses the series ticker (one steward alias families a whole product line), Polymarket uses Gamma's `sportsMarketType` when stamped (dictionary maps "moneyline"→h2h) else the event title. 11 new `prediction.*`/`social.*` capability labels; X (Twitter) is a research surface, not a priced book — no feed. **Registry: 25 feeds (5 tiers).**
- [x] **Exchange-vs-book CLOSED (PR #10 era, live-verified)**: kalshi GAME-series sport rides the series ticker (KXNBAGAME → basketball — the generic "Sports" category never met any book's bucket), matchup titles reduce to "X vs Y", expected_expiration ≈ game end is the start proxy, " At "/" at " joined the resolver separators (sportsbet's US-league naming had been silently fixture-splitting), "basketball - us" aliased onto basketball. **Live: NBA Finals Game 5 fixture carries betr, fanduel, tab, unibet, sportsbet AND kalshi.** Steward convention documented in its spec: KX*GAME → h2h, futures/awards series get their own family.
- [x] **site_manager (6th ops agent)** + news_scout (product, social.*/content.news); weekly site cron Mon 10:00; OPS_SLACK_CHANNEL wired and push-verified; analytics loader on the site is opt-in (`window.ANALYTICS_URL`, GoatCounter-style, null = no tracking) — create the account and set the endpoint to get real page views; `scripts/record-demo.py` re-records the demo fallback from real gateway runs.

---

## Phase P4 — Productize — REPLANNED as the desktop pivot (see [`P4_DESKTOP_PLAN.md`](./P4_DESKTOP_PLAN.md)) — gated on go/no-go + legal (`D13`)

**Goal (revised 2026-06-12):** a downloadable desktop app — the agent harness on the
user's machine (their compute/storage/files, their own odds capture, BYO model key),
a bundled chat UI, no central hosting of user data. Milestones M4.1–M4.5 in the plan
doc supersede the SaaS milestones below; the hosted-tenant design is RETAINED below
for a possible future cloud tier (the tenancy seams stay in the code).

- [x] **M4.1 — Daemonize (v0.14.0)**: `paths.py` (OS-conventional storage — macOS
  `~/Library/Application Support/sportsdata/`, Windows `%APPDATA%`, Linux XDG; the
  config DB default flipped from Postgres to the durable SQLite warehouse there;
  scheduler logs, ops state, locks, backups, user specs all moved off `/tmp` and
  `~/.sportsdata-agents`; `migrate_legacy_layout` does the one-time move). Keychain
  secrets (`keyring` tier in resolve_secret: env → keychain → map). `agents setup`
  (provider pick → live-verified key → keychain). `agents app` supervisor (gateway
  + the conductor loop in ONE process — no crontab, no .env). **Live exit gate:
  fresh OS data dir, no .env, no /tmp → kalshi capture 12,842 snapshots into
  ~/.../sportsdata/warehouse.db; the supervisor booted the gateway, served /healthz
  + /demo/prompts, shut down clean.** macOS→Windows order; BYO-key; app name
  "sportsdata". Mac packaging/signing is M4.3. Chat UI deferred (web-app-later).

**Original SaaS goal (deferred):** a second tenant on a paid tier with isolated data, enforced entitlements + budgets.

- [ ] **Kalshi structured targets → player-prop resolution** (carried from the P3 reviews):
  Kalshi's entity registry (`kalshi_structured_targets` — players, teams, companies with
  source ids) can anchor PLAYER-level resolution the way fixtures anchor match-level —
  player-prop markets across books joined to one entity, prop arbs and prop CLV on top.
  Build when player props become a product surface; no consumer before then.

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

### Operations — catalogue steward (decided 2026-06-10)
- [ ] **(P3) Catalogue schema-mapping by ops agents (layer 3)** — when a weekly harvest returns zero ids for a book through both detection layers (key conventions → value inference), an ops agent reads a truncated payload sample ONCE, emits the (name, id) field mapping, and opens a PR caching it in the catalogue — LLM judgment for the open world, deterministic execution thereafter (P8).
- [ ] **Weekly book-catalogue refresh** — *lives in THIS repo* (the consumer owns the cache; the MCP stays a stateless proxy): `agents refresh-books`, a deterministic CLI (no LLM) that probes each book's **discovery** routes through the MCP (sportsbet classes→competitions, pointsbet sports list, tab sports tree, entain categories…) and rewrites the auto-generated section of `skills/book_navigation/SKILL.md` (verified competition ids, market names, naming conventions). Schedule weekly (cron/`/schedule`). At P3 the **ops agents** take over running it + opening a PR when ids drift; at P2 the ingestion store supersedes most of it for odds.
- [ ] *(In `sportsdata-mcp`, done 2026-06-10)*: weekly GitHub Actions cron runs the live **contract suite** (structure drift on globally-reachable providers; AU books skip on GitHub runners and are covered by local runs).

### Testing
- [ ] Unit (tools, gateway, harness, loader) · integration (flows vs local MCP) · contract (agent registration + typed-output shape) · eval (accuracy/calibration/CLV) · isolation (multi-tenant).
- [ ] CI default `-m "not live and not eval"`; nightly job runs `live` + `eval`.
- [x] **(P1)** Add a Postgres service container job to CI so migrations + queries are tested on the prod dialect, not just SQLite (JSON/JSONB, timezone semantics). *(Done 2026-06-10 with the P1 review fixes: `test-postgres` job + `TEST_DATABASE_URL` fixture — name must contain "test"; schema dropped/recreated per test.)*
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
