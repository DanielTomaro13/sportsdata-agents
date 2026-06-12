# Repository structure

Top level:

| Path | What |
|---|---|
| `src/sportsdata_agents/` | the package (everything below is under here) |
| `tests/` | `unit/` (offline, deterministic) + `integration/` (DB-backed, `-m integration`) |
| `scripts/` | build/release helpers (desktop bundle, sign, data-bundle, licence keygen, site deploy) |
| `packaging/macos/` | the `.app` launcher, Info.plist, entitlements |
| `docs/` | this documentation set |
| `site/` | the marketing site source (published to the public `sportsdata-site` repo by `scripts/deploy-site.sh`) |
| `alembic/` | DB migrations |
| `*.md` (root) | PLAN / BUILD_PLAN / P4_DESKTOP_PLAN (history), PRICING, POST_DEV, RELEASE, README |

## `src/sportsdata_agents/`

| Module | Responsibility |
|---|---|
| `agents/` | the agent runtime, spec loader/models, harness (`ToolDef`, run loop), skills loader, capability label catalogue |
| `specs/` | the agent specs (one YAML per agent) — the product + ops roster |
| `skills/` | `<name>/SKILL.md` domain playbooks agents load just-in-time |
| `tools/` | native (in-process) tools: `registry` (quant + desk), `quant`, `desk`, `tracking`, `monitoring`, `arbitrage`, `dictionary`, `resolution`, `builder`, `ops`, `memory`, `slack_admin` |
| `gateway/` | the FastAPI gateway (`app.py`), the web chat UI (`ui/`), conversation store, tasks/SSE, demo surface |
| `app/` | the desktop supervisor (`supervisor.py` = gateway + conductor) and the setup wizard |
| `operations/` | the scheduled engine: `ingestion/` (capture→warehouse), `resolution/` (events→fixtures + market dictionary), `monitoring/` (watches→alerts), custodian, `migrate`, `datafeed` (OTA), `refresh_books` |
| `quant/` | deterministic math: vig removal, value, backtest, calibration metrics, lineup, arbitrage |
| `licensing/` | `license` (Ed25519 tokens), `entitlements` (tiers/add-ons), `enforce` (the seams), `billing` (webhook → issue) |
| `data/` | SQLAlchemy models, engine/session, repository (tenant scope) |
| `mcp/` | the client/manager that launches and talks to the `sportsdata-mcp` subprocess |
| `sandboxes/` | the `run_python` sandbox (local subprocess; e2b optional) |
| `interfaces/` | `cli/` (the `agents` command), `discord/`, Slack adapter |
| `observability/` | tracing + the run recorder (progress hooks) |
| `paths.py` | OS-conventional storage locations |
| `secrets.py` | keychain-backed secret resolution |
| `config.py` | settings (env-driven) |

## How an agent is defined

A spec (`specs/<id>.yaml`) declares: `id`, `plane` (product/ops), `model_tier`,
`system_prompt`, `tools.mcp_capabilities` (data-plane tags) + `tools.native`
(in-process tools), `skills`, `can_delegate_to` (orchestrator only), and `limits`.
The loader validates every reference; `agents lint` is the gate. Adding an agent is
a new YAML file — no code. See [AGENTS.md](AGENTS.md).

## Tests & gates

`ruff check .` · `mypy` · `agents lint` · `pytest -m "not live and not eval"`.
CI runs all of these on every PR (plus an integration job on real Postgres).
