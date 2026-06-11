# Managing models, limits and budgets

Four layers control which model runs and how much it may spend. Each layer only
narrows the one above it — nothing escalates.

## 1. Which models exist — `src/sportsdata_agents/models/policy.yaml`

The tier map. **Config, not code** — edit and restart:

```yaml
tiers:
  fast:      { default: "anthropic/claude-haiku-4-5",  fallback: "openai/gpt-4o-mini" }
  balanced:  { default: "anthropic/claude-sonnet-4-6", fallback: "openai/gpt-4o" }
  strong:    { default: "anthropic/claude-opus-4-8",   fallback: "openai/gpt-4o" }
```

Model ids are litellm ids (`provider/model`). `fallback` is used when the
primary errors. Swap a tier's default here to change it for every agent at once.

## 2. Which key is used — `.env` (BYO-LLM, §8.1)

The first configured key wins, in this order:
`ANTHROPIC_API_KEY` → `OPENROUTER_API_KEY` → `GEMINI_API_KEY` → `GROQ_API_KEY`
→ `OPENAI_API_KEY`. A non-Anthropic key pins **every tier** to that provider's
mapped model (e.g. Groq → `groq/openai/gpt-oss-120b`) so a single free-tier key
runs the whole team.

One-off override from the CLI — pins all three tiers for that run only:

```bash
agents run "..." --model anthropic/claude-haiku-4-5
agents ops run repo_improver "..." --model openrouter/openai/gpt-4o-mini
```

## 3. Which tier an agent uses — its spec

Every agent spec names a tier (or an explicit model):

```yaml
agent:
  model_tier: fast          # fast | balanced | strong, resolved via policy.yaml
  # model_tier: "anthropic/claude-opus-4-8"   # or pin one model explicitly
```

Current assignments: orchestrator/specialists mostly `balanced`; cheap
formatters and watchers (`bet_notifier`, `mcp_health`, `incident_triage`,
`eval_benchmark`, `value_scout`, `market_steward`) are `fast`; `modelling`,
`repo_improver`, `code_reviewer` are `strong`. The agent-builder exposes tiers
to users as **Fast / Balanced / Smart**.

## 4. How much it may spend — spec limits, clamped by workspace budgets

Per-run ceilings live in each spec:

```yaml
  limits:
    max_tool_calls: 15
    max_steps: 20
    max_tokens: 60000        # cumulative across the run
    timeout_seconds: 120
    cost_ceiling_usd: 0.20   # the run STOPS here (stop_reason=budget_exhausted)
```

The workspace clamps everything (a spec can never exceed its workspace):

```python
Workspace(budgets=Budgets(per_run_usd=0.50, monthly_usd=100.0,
                          max_tool_calls=50, max_steps=40,
                          max_tokens=120_000, timeout_seconds=300))
```

One `RunBudget` is shared per **team** run — delegations charge the caller's
ceiling, so "per-run" means the whole tree, not per agent. The demo surface uses
its own tiny workspace (`$0.30/run`); ops agents carry their own ceilings
(improver `$1.00`, reviewer `$0.80`).

## Where to see what was spent

- every CLI run prints `cost=$0.xxxx` in its footer;
- `agent_runs` / `usage_ledger` tables record tokens + cost per run and call
  (when the DB is up);
- `agents ops run eval_benchmark "..."` writes `agent_metrics` rollups.

## Quick recipes

| I want to… | Do this |
|---|---|
| Make everything cheaper | put a Groq/Gemini key first in `.env`, or edit the three tier defaults |
| Make one agent smarter | bump its spec's `model_tier` to `strong` (bump `version` too — D27) |
| Cap a runaway agent | lower `cost_ceiling_usd` / `max_steps` in its spec |
| Cap a whole workspace | lower `Budgets.per_run_usd` / `monthly_usd` |
| One expensive run, once | `--model anthropic/claude-opus-4-8` on that command only |
| Pin a workspace to an old agent version | `Workspace.agent_versions = {"value_scout": "0.1.0"}` |
