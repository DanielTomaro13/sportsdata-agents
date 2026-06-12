# Architecture & system design

`sportsdata-agents` is a **local-first agentic platform** over a sports data plane.
The user prompts; the LLM agents do the work; the user orchestrates and pays for
the tier they're on. It runs as a **downloadable desktop app** — the user's
compute, the user's storage, the user's odds capture, their own model key.

## One sentence

The app is a **supervised process tree** — UI shell → local gateway (chat API +
SSE) → agent runtime → MCP data-plane subprocess → SQLite warehouse — all on the
user's machine, with the **conductor** as its heartbeat.

```
 ┌─────────────────────────────────────────────────────────────┐
 │  sportsdata.app  (or `agents app`)                           │
 │                                                              │
 │   browser chat UI ──HTTP/SSE──►  gateway (FastAPI, 127.0.0.1)│
 │                                     │                        │
 │                                     ▼                        │
 │                              agent runtime  ◄── specs (YAML) │
 │                                 │      │                     │
 │                    native tools │      │ MCP capabilities    │
 │                   (quant, desk, │      ▼                     │
 │                    tracking…)   │   sportsdata-mcp subprocess│
 │                                 ▼      (stdio)               │
 │                          SQLite warehouse  ◄── conductor loop│
 │                          (odds history,        (ingest /     │
 │                           models, alerts)       resolve /    │
 │                                                 monitor /    │
 │                                                 custodian)   │
 └─────────────────────────────────────────────────────────────┘
   secrets: OS keychain   ·   licence: Ed25519 token (offline)
```

## The three planes

The system is organised as three agent "planes" that never blur:

- **Data plane** — `sportsdata-mcp`, a separate MCP server launched as a stdio
  subprocess. It exposes ~60 **capabilities** (sport prices, stats, racing,
  prediction markets, social, news…). Agents declare the capabilities they need
  (`mcp_capabilities` in their spec); the runtime resolves those to MCP tools.
- **Product plane** — the customer-facing agents (orchestrator + specialists:
  odds, stats, racing, prediction markets, modelling, value, arb, fantasy,
  tracking, bankroll, news…). See [AGENTS.md](AGENTS.md). Subject to entitlements.
- **Ops plane** — the platform's own maintenance team (mcp_health, incident_triage,
  repo_improver, code_reviewer, eval_benchmark, site_manager, docs_keeper). NEVER
  gated by a licence; runs the self-improvement loop (telemetry → CI-gated PRs a
  human merges).

## The conductor (heartbeat)

`agents app` runs the gateway **and** an in-process conductor loop (`run_tick`
every 60s) in one supervised process — no crontab on user machines. The conductor
drives the scheduled jobs: **ingest** (capture odds → change-point warehouse),
**resolve** (book events → fixtures), **monitor** (line/arb/value watches → push
alerts), **custodian** (disk-aware backups/retention). A crash in any child is
restarted with exponential backoff; a bad tick is logged and the loop survives.

## Storage (OS-conventional, durable)

Everything persistent resolves through `paths.py`:
- macOS `~/Library/Application Support/sportsdata/`, Windows `%APPDATA%`, Linux XDG.
- `warehouse.db` (odds history, models, predictions, alerts), `backups/`, `specs/`
  (user-built agents), `skills/`, `logs/`, `data-overlay/` (OTA data), the
  **desk folder** (agent exports the user opens). Override with
  `SPORTSDATA_AGENTS_DATA_DIR`. Nothing lands in `/tmp`.

## Secrets

Model keys and book/exchange credentials live in the **OS keychain** (`keyring`),
not a file. Resolution order: env → keychain → caller map. The first-run wizard
(`agents setup`) verifies a key with a live call, then stores it.

## Licensing & entitlements (offline)

A licence is an **Ed25519-signed token** (`<payload>.<sig>`) verified offline
against a public key baked into the build (`SPORTSDATA_LICENSE_PUBKEY`); the
private key issues licences (the billing webhook). Tiers (free/base/plus/pro) +
add-ons resolve to an `Entitlements` set, enforced at **existing seams**: the team
roster is filtered, the MCP group list is capped, chat-UI/app/channel features are
gated. **Invariant:** no baked pubkey (source/dev build) = unrestricted; a product
build with the pubkey enforces; a missing/expired/tampered licence fails OPEN to
the free tier — never locked out. Ops agents are never gated. See [../PRICING.md](../PRICING.md).

## Security posture (desktop daemon)

- The gateway binds `127.0.0.1` and **rejects foreign `Host` headers** (DNS-rebinding
  defense — a web page can't drive the local agent). `SPORTSDATA_GATEWAY_TOKEN`
  adds a bearer-token gate on mutating requests; `SPORTSDATA_GATEWAY_ALLOW_HOSTS`
  extends the allowlist for advanced binds.
- `--demo-only` is the only internet-facing mode (curated demo prompts + leads,
  no header-trusted routes, its own gate).
- No agent ever places a bet or moves money — advisory only, enforced in code
  (money-verb tools are denied), not just prompts.
- Egress is only model-API calls + the user's own Slack/Discord + the OTA fetch
  the user runs. Bets, models and bankroll notes never leave the machine.

## Channels

The same gateway surface serves the **web chat UI**, **Slack** and **Discord**.
Conversational features are identical across channels; rich/interactive surfaces
(streaming tool feed, boards) are app-only; push alerts go everywhere.

## Distribution & updates

- App: a signed, notarized macOS DMG (the "daemon + browser UI" model). See
  [../RELEASE.md](../RELEASE.md) and [UPDATING.md](UPDATING.md).
- Data ships as data: the market dictionary + capability labels update **OTA**
  between releases via a signed bundle (`agents update-data`).
