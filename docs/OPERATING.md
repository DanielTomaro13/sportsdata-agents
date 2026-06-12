# Operating the platform

How you (the owner/operator) configure, monitor, and control the platform. The
operator surface is CLI-first — everything below reads data the platform already
produces; nothing needs a hosted admin panel.

## Backend configuration

Config lives in three layers: `SPORTSDATA_AGENTS_*` env vars + `.env`
(`config.py`), secrets in the **OS keychain** (model keys via `agents setup`; the
rest as env), and two behaviour-as-data files — `models/policy.yaml` (tier→model)
and `operations/scheduler.py` `JOBS`. To see it all at once and what's missing:

```sh
agents config            # inventory + validate every setting/secret, grouped
agents config --verify   # also make one live model call to test the key
```

It groups checks into **Core** (must work to run), **Security**, **Licensing**,
**Commercial** (to take payments), and **Updates** — each ok / warn / missing.

## Operator vs customer (your ops agents run only for you)

The ops plane (mcp_health, incident_triage, repo_improver, eval_benchmark,
site_manager, docs_keeper, code_reviewer) is **owner-only by construction**: the
customer gateway physically cannot open an ops agent, and the only path that
injects ops tools + platform creds is `agents ops run <agent>`.

The **scheduled** ops jobs, the self-healing handoff, and the in-app operator panel
are gated by `is_operator()`. This is **cryptographic on a release build** — it
requires a signed licence carrying the `operator` claim, which only you can mint
(it needs the private signing key). Issue yourself one and activate it:

```sh
# on the issuer box (holds SPORTSDATA_LICENSE_PRIVKEY)
SPORTSDATA_LICENSE_PRIVKEY=... python scripts/license.py issue \
    --tier pro --to you@example.com --operator --days 0   # 0 = perpetual
# on your deployment
agents license --activate <the-token>
```

On a **source checkout** (no public key baked) `export SPORTSDATA_OPERATOR=1` still
works as a local convenience — but on a shipped build that env var is **ignored**,
so a customer cannot grant themselves operator access. See
[Security & cost controls](SECURITY.md#3-operator-access--only-the-product-owner).

With operator mode on, the conductor runs the full job set incl. platform-maintenance
(site_manager, eval_benchmark, refresh_books, ops_health) and the self-healing
incident_triage→repo_improver handoff. **Off (the default on a customer install)**
the conductor runs only the data plane: ingest, monitor, custodian, resolve,
results, steward. A customer never runs your maintenance.

### Tracking what your ops agents are doing

```sh
agents ops status        # recent ops runs, open escalations, disabled feeds, job status
agents ops health        # deterministic: MCP doctor + feed freshness + site (no LLM)
agents ops run <agent> "<task>"   # run one ops agent by hand (PRs only; you merge)
```

## Costs & models

Every model call is metered into `agent_runs` (cost, tokens, model, tier, agent,
plane). Ops spend is tenant `platform`; product spend is everything else.

```sh
agents costs                       # spend by day/agent/model, ops vs product, last 7d
agents costs --days 30
agents costs --set-budget 50 --period monthly    # set a cap — ENFORCED, not just reported
```

The budget is a hard ceiling: once the period's spend reaches it, the model gateway
refuses further calls (runs end as `budget_exhausted`) until the period rolls over —
covering both your ops maintenance and product spend. Each run is independently
capped by its per-run ceiling. Details:
[Security & cost controls](SECURITY.md#1-cost-controls--nobody-exceeds-their-budget).

- **Right model per tier:** edit `models/policy.yaml` (today Haiku/Sonnet/Opus with
  GPT-4o fallbacks). Pin a specific model on an agent via its `model_tier`.
- **Right agent for the work:** the orchestrator picks the model *class* per
  delegation; `eval_benchmark` produces the `delegation_stats` routing-economics
  report + `agent_metrics` rollups; per-agent `cost_ceiling_usd` hard-clamps a run.

## What to monitor to stay healthy & current

| Concern | Check |
|---|---|
| Data freshness (feeds capturing) | `agents ops health` (stale feeds) |
| Disk / retention | the custodian (hourly); `agents ops status` |
| Data plane (sportsdata-mcp) | `agents ops health` (doctor + contract suite) |
| The daemon is up | the supervisor restarts children on crash; watch logs |
| Spend & model errors | `agents costs`; the budget breach flag |
| Commercial path | the billing webhook + licence-refresh endpoint reachable |
| Updates | app releases ([RELEASE.md](../RELEASE.md)); OTA data (`agents update-data`) |

## Updating

See [UPDATING.md](UPDATING.md) — three channels: OTA data (`agents update-data`),
app releases (tag → signed DMG), and the contributor version flow.
