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

### Quant ideas backlog (from the P2 review — future milestones)
- [ ] **market_monitor agent** (M3.x ops): scheduled value scans (model predictions × fresh prices → `value_finder`) pushing alerts via `push_notification` — "value alerts fire" becomes push, not pull.
- [ ] **ratings_keeper**: maintain per-team Elo/power ratings as a persistent feature store the modelling agent reads (P8: deterministic updates, LLM only narrates).
- [ ] **Results ingestion feed**: `event_results` currently fills via `record_result`; a scheduled feed (scoreboard → winner) closes the backtest loop automatically.
- [x] **More odds feeds** *(EVERY viable provider done 2026-06-11)*: **14 feeds across 8 providers** — NBA CDN (multi-book), Sportsbet + TAB + Unibet/Kambi + BetR + Entain(Ladbrokes) + **Pinnacle** (the sharp CLV benchmark; American→decimal) + PointsBet, each AFL+NRL where carried. One normalizer per provider (shapes captured live: Sportsbet top-level list/resultType; TAB propositions/position; Kambi milli-odds + `FULL_TIME_OVERTIME` for NRL; BetR flat outcome rows; Entain UUID-joined maps with fractional odds and " vs " names; Pinnacle matchups+markets via a **multi-call fetcher** (`Feed.fetch` seam); PointsBet ~5MB per-event details → 900s cadence). Ingest subprocess lifts the MCP byte cap to 8MB (`INGEST_MAX_BYTES` — the 150KB default guards model contexts; ETL has none). **Honestly skipped:** Betfair — the public readonly key returns no `RUNNER_EXCHANGE_PRICES_BEST` sections from AU even on a $26K-matched market (verified live); fetcher+normalizer are ready for an authenticated Exchange key (P4). FanDuel US sportsbook adds nothing the NBA CDN feed doesn't carry. Live: one `agents ingest --once` → 13/13 ✓, warehouse census 13 books × 3 sports, 604 snapshots. Cross-book event RESOLUTION (same match, different ids per book) is the next warehouse problem (fixtures/events tables).
- [ ] **injury/news scout** (P4, after §13 prompt-injection guardrails — untrusted third-party text).
- [ ] **Calibration-curve artifact**: modelling agent saves a reliability diagram PNG with each model version (artifacts now reach Slack).

- [x] **🚪 P2 EXIT GATE — CLOSED** (2026-06-10): end-to-end quant loop green on one warehouse, every stage the real implementation (`test_p2_exit_gate.py`): two ingest captures (entry 2.10 → close 1.90) → model calibrated on holdout (Brier 0.19) + persisted + predictions recorded → value_finder flags home at **+26% edge** (the computed alert) → result settles, backtest replays: 1 qualifying bet, P&L +1.10, **avg CLV +10.53% > 0**, below-edge skip counted → offline eval gate green on baseline. Live legs verified separately: real NBA feed captured twice with dedupe (M2.1), all 4 `-m eval` cases with the real model (M2.4). **Quant caveat, stated plainly:** the exit gates prove the MACHINERY (capture→calibrate→edge→CLV→gate); profitable models are an ongoing practice, not a milestone — golden datasets pin the math, the weekly eval gate watches for regressions.

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
