# SportsData — Product & Production Review

_Snapshot: 2026-07-02 · sportsdata-mcp v0.17.1 · sportsdata-agents v0.37.0_

A single, in-depth reference covering **what the service is**, its **capabilities**,
its **intended audiences**, the **go-forward plan**, a **security analysis**, and the
**remaining work to launch broadly**. Written from a full read of both repos plus the
commerce Worker and marketing site.

---

## 1. What the service is

SportsData is a **commercial sports-data intelligence platform** shipped as two
products that share a spine:

1. **`sportsdata-mcp`** — a downloadable **Model Context Protocol (MCP) server** that
   turns 28 live sports-data and betting sources into **492 typed tools** any MCP
   client (Claude Desktop, Cursor, etc.) can call. This is the *data plane* and the
   thing customers actually buy and install.

2. **`sportsdata-agents`** — an **agentic analytics platform** built on top of that
   data: 28 specialised agents, a quant/odds-history warehouse, a local "workbench"
   desktop app, and the **commerce backend** (a Cloudflare Worker + Stripe) that sells
   and provisions the MCP.

The commercial model is **"the licence is the feed list."** A customer subscribes via
Stripe for N data feeds, receives an **offline-verifiable Ed25519 entitlement token**,
and the MCP unlocks exactly those feeds — no server round-trip at query time, no
account to log into, works fully offline once installed. A third public repo,
`sportsdata-site`, is the GitHub-Pages publish target for the marketing site.

**In one line:** *a paid, install-and-own MCP that exposes ~500 sports/betting data
tools, plus an agent platform and a Stripe-backed storefront that provisions it.*

---

## 2. Architecture at a glance

```
                    ┌─────────────────────────────────────────┐
  Customer's        │  MCP client (Claude Desktop / Cursor)   │
  machine           │            │ stdio                       │
                    │  sportsdata-mcp  (Python, FastMCP)      │
                    │   • 28 provider YAML specs → 492 tools  │
                    │   • licence gate (Ed25519, offline)     │
                    │   • signed OTA spec overlay             │
                    └───────────────┬─────────────────────────┘
                                    │ HTTPS (public + credentialed feeds)
                                    ▼
        ┌───────────────────────────────────────────────────────────┐
        │  Cloudflare Worker  (services/entitlement, TypeScript)     │
        │   /stripe/webhook  /entitlement  /assignment               │
        │   /download        /proxy (DataGolf/TAB key-pool)          │
        │   D1 (SQLite): customers · entitlements · stripe_events    │
        │   Ed25519 signer · SHA-256 key hashing · HMAC dl tokens    │
        └───────────────┬───────────────────────────┬───────────────┘
              Stripe ────┘                           └──── Resend (fulfilment email)

  Operator's machine (owner only)
        ┌───────────────────────────────────────────────────────────┐
        │  sportsdata-agents  (Python)                               │
        │   • agent harness + 28 agents (product + ops plane)        │
        │   • quant: arb / value / backtest / CLV                    │
        │   • odds-history warehouse (SQLite / TimescaleDB)          │
        │   • ingestion + resolution + monitoring/alerts             │
        │   • gateway daemon + browser "workbench" UI (desktop app)  │
        │   • operator plane (site/repo/eval self-maintenance)       │
        └───────────────────────────────────────────────────────────┘
```

Four **independent Ed25519 signing systems** run across this (same wire format,
different keys/jobs): operator-licence, feed-entitlement, MCP spec-OTA, agents
data-OTA. See §7.

---

## 3. Product 1 — `sportsdata-mcp` (the data plane)

**What it is.** A spec-driven MCP server. Each provider is a declarative YAML file in
`src/sportsdata_mcp/specs/`; a loader turns each spec's routes into FastMCP tools at
startup and serves them over stdio. Adding a data source is *writing a YAML file*, not
writing code — this is the core design win and why coverage grew so fast.

**Coverage — 28 providers, 84 groups, 492 tools:**

| Category | Providers |
|---|---|
| **AU/global sportsbooks** | Sportsbet, TAB, PointsBet, BetR, Unibet, Entain, Betfair (exchange), Pinnacle, Dabble, FanDuel |
| **Prediction markets** | Kalshi, Polymarket |
| **Official leagues** | AFL (+ Champion Data), NRL, NBA, NBL, MLB, Premier League, La Liga, Serie A, Cricket Australia |
| **Racing** | RacingAndSports, plus racing surfaces inside Sportsbet/TAB/PointsBet/FanDuel/Unibet |
| **Specialist / data** | DataGolf (paid key), OpenF1 (telemetry), WTA (tennis), ESPN (multi-sport CDN), SuperCoach (fantasy), Twitter/X (trends, tweets, users) |

**Dispatcher kinds** (`src/sportsdata_mcp/dispatchers/`): `templated_rest` (the workhorse
REST templater), `graphql_persisted` (persisted-query books like Sportsbet), and
`graphql_query` (ad-hoc GraphQL like Unibet). A shared HTTP client handles auth schemes,
retry, and rate limiting.

**Key subsystems:**
- **Licence gate** (`licence.py`) — reads a signed entitlement token, verifies it
  offline against a baked public key, and enables exactly the licensed provider groups.
  Fail-open to a free tier on a missing/invalid token; never validates a tampered one.
- **OTA spec overlay** (`ota.py`) — signed provider-spec updates can be pushed without
  a new binary release (mandatory digit-led version, all-or-nothing apply, name
  whitelist, 8 MB cap, scheme restriction). This is how a book's endpoint change gets
  patched in the field.
- **Refresh tooling** (`refresh/`) — re-derives persisted-query hashes when a book
  rotates them.
- **Reference resources** (`resources/`) — 9 MCP resources (enums, capability maps).

**Distribution.** PyInstaller one-dir build → macOS `.app` wrapper (and a Windows build
job). `setup` command writes Claude Desktop + Cursor configs automatically. A verified
end-to-end purchase registered **19 groups / 80 tools / 9 resources** for a 5-feed
licence.

**Test/quality posture.** 6 test modules, `ruff` lint, version-sync tests (both
`__init__.py` and `pyproject.toml` must match), tag-triggered release workflow. **Zero
TODO/FIXME/HACK markers** in the source.

---

## 4. Product 2 — `sportsdata-agents` (the platform)

**What it is.** The umbrella platform that consumes the MCP data and adds reasoning,
storage, monitoring, and the storefront. Python, 67 test modules.

### 4.1 Agent harness & roster
A model-agnostic harness (`agents/harness.py`, `runtime.py`) runs a tool-calling loop
with a hard **per-run budget ceiling** and an optional **period budget**, routes models
through a `ModelGateway` (`models/gateway.py` + `policy.py`) by tier, and supports
agent-to-agent delegation with a recorded delegation tree. **28 agents** ship, split
into two planes:

- **Product plane** (customer-facing): `orchestrator`, `concierge`, `odds_specialist`,
  `value_scout`, `arb_hunter`, `racing_analyst`, `stats_specialist`, `modelling`,
  `backtester`, `fantasy_advisor`, `prediction_market_analyst`, `news_scout`,
  `bet_tracker`, `bet_notifier`, `bankroll_manager`, `data_analysis`, `generalist`,
  `agent_builder`.
- **Ops plane** (operator-only maintenance): `market_steward`, `site_manager`,
  `docs_keeper`, `repo_improver`, `code_reviewer`, `incident_triage`, `mcp_health`,
  `eval_benchmark`, `slack_manager`, `policy`.

Agents draw on **13 reusable skill packs** (vig removal, calibration, backtest design,
racecard reading, DFS lineup building, etc.).

### 4.2 Quant & data plane
- **Odds-history warehouse** (`data/models.py`): `Price`, `Event`, `EventResult`,
  `Alert`, subscriptions — runs on SQLite locally or **TimescaleDB** at scale, with
  additive `ensure_schema` migrations + Alembic.
- **Ingestion** (`operations/ingestion/`): fetch → normalize → store, generic across
  all books (no hardcoded market/sport names).
- **Quant** (`quant/`): arbitrage detection, value-finder, backtesting, closing-line
  value (CLV) with proper de-vigging.
- **Resolution** (`operations/resolution/`): maps book events onto canonical fixtures
  so prices across books line up.
- **Monitoring** (`operations/monitoring.py`): watch engine for `arb`, `line_move`, and
  `value` alerts, with push delivery.

### 4.3 Gateway / workbench (the desktop app)
A local FastAPI daemon (`gateway/app.py`) serves a no-build browser **workbench** UI
(`gateway/ui/`) with panes for chat, agents, monitors, files, settings, and an
operator-only console. Recent work added provider on/off toggles, chat-reply reasoning
traces, and the monitors pane. Packaged as a downloadable Mac/Windows desktop app
(BYO model key).

### 4.4 Interfaces
CLI (`interfaces/cli`), plus Discord and Slack adapters (`interfaces/`) gated as paid
add-ons.

---

## 5. Commerce & distribution (the money path)

Implemented as a **Cloudflare Worker** (`services/entitlement/`, TypeScript, deployed at
`api.sportsdata-ai.com`) backed by **D1** (SQLite), Stripe, and Resend.

**End-to-end flow:**
1. Customer buys on the site via a **Stripe Payment Link** (subscription; SKUs/prices in
   `catalogue.ts` + `scripts/setup-stripe.py`).
2. **`/stripe/webhook`** verifies the Stripe signature, writes a customer + entitlement
   row to D1, and fires a **fulfilment email** (`email.ts` + `config-gen.ts`) with an
   OS-aware setup guide and a signed download link.
3. Customer picks feeds via **`/assignment`** (the feed picker; Stripe tracks the *count*,
   D1 tracks *which* feeds).
4. **`/entitlement`** issues the signed Ed25519 token the MCP verifies offline.
5. **`/download`** proxies the private GitHub release binary behind an **HMAC download
   token** (7-day TTL), OS-aware asset selection, with an R2 scaffold for later.
6. **`/proxy`** fronts credentialed feeds (DataGolf, TAB) using a server-side **key pool**
   so the customer never needs the paid upstream key; the cache key strips the secret.

**Security-relevant commerce facts:** licence keys are **SHA-256 hashed at rest** in D1;
webhook events are idempotent (`stripe_events` table); CORS is allow-listed;
the binary cannot be fetched unpaid (verified: unauth release GET → 404).

**Storefront** (`site/`): `index.html`, `feeds.html` (picker), plus `privacy`, `terms`,
`refunds` pages. Generated from `sportsdata-agents/site/` and published to the public
`sportsdata-site` repo (never hand-edit the public repo — it is clobbered on publish).

---

## 6. Purpose & intended audiences

**Purpose.** Collapse the two hardest parts of sports-data work — *getting clean, live,
multi-book data* and *reasoning over it* — into an install-and-own product, monetised
per data feed, that runs on the customer's own machine and their own model key.

**Primary audiences:**

| Audience | What they get |
|---|---|
| **Quant / sharp bettors & traders** | Multi-book price capture, arbitrage & value detection, CLV, backtesting, line-move alerts |
| **Sports/DFS analysts & content makers** | Official league stats, fantasy (SuperCoach), racing form, news scouting |
| **AI builders & MCP power users** | 492 typed tools behind one MCP install — drop-in sports data for their own agents/apps |
| **The operator (owner)** | The ops plane: a self-maintaining platform (site, repos, evals, catalogue) gated to exactly one cryptographic identity |

**Positioning note.** The product is **advisory only** — no agent places a bet or moves
money. That's a deliberate legal/ethical boundary baked into the agents' policy.

---

## 7. Security analysis — risks & vulnerabilities

The platform has a **documented, code-backed threat model** (`docs/SECURITY.md`) and is
genuinely well-secured for a local-first BYO-key product. The honest framing below
separates *hard guarantees* from *known, accepted limits*.

### 7.1 What's solid
- **Four independent Ed25519 systems**, each verifying offline against a baked public
  key, each with its own private issuer key that never ships:
  operator-licence · feed-entitlement · MCP spec-OTA · agents data-OTA.
- **Cost is hard-capped** at the one chokepoint every model call passes through:
  per-run `RunBudget` (always constructed) + optional enforced `PeriodBudgetGuard`.
  Overshoot is bounded to a single in-flight call.
- **Operator access is cryptographic, not a flag.** On a release build the
  `SPORTSDATA_OPERATOR` env var is *ignored*; `/operator/*` routes return **404** for
  anyone without a signed `operator` claim. The private key needed to mint one never
  ships.
- **Daemon hardening:** loopback-only `Host` guard (blocks DNS-rebinding),
  constant-time (`hmac.compare_digest`) token on state-changing routes.
- **Commerce integrity:** Stripe webhook signature verification, parameterised D1
  queries, idempotent event handling, SHA-256 key hashing at rest, HMAC-gated download,
  CORS allow-list, key-pool proxy that never leaks the upstream secret.
- **Prompt-injection stance:** model/tool/web/file output is treated as **data, never
  instructions**; no agent executes instructions found in observed content.
- **Kid-aware key rotation** across three of the four systems — no flag-day cutover.
- **Clean tree:** zero TODO/FIXME markers; secrets git-ignored; public keys (not private)
  are what's baked into the build.

### 7.2 Accepted limits (by design, documented)
- **Entitlement is revenue protection, not DRM.** A customer who edits the source on
  their own machine can bypass the feed-gate seams. This is inherent to any local app;
  the *cost* layer above it (spend on their own key) and the *operator* surface (signed,
  not editable) are the hard boundaries. **Acceptable** for the current model.
- **Period-budget concurrency:** two processes spending in the same window can exceed the
  cap by at most one run's ceiling. Negligible for single-user installs; revisit if a
  server/multi-tenant mode ships.
- **Managed mode not shipped:** today everyone is BYO-key, so a `$0`-metered (unpriced)
  model costs the operator nothing. **If a managed tier ever proxies calls on the
  operator's keys, it MUST allow-list priced models first** — the `cost_known=False`
  flag is already in place to enforce on. This is the single most important
  "don't-forget-before-scaling" item.

### 7.3 Residual risks to watch (ranked)
1. **Unsigned desktop build (HIGH friction, not a breach).** No Apple notarization / no
   Windows Authenticode → Gatekeeper/SmartScreen scare users at install (the `xattr`
   dance). Not a vulnerability, but a real conversion and trust cost. *Needs the paid
   Apple + Windows certs — owner action.*
2. **Single points of failure in the money path (MEDIUM).** D1 is the one source of
   entitlement truth, and there is effectively **one shared DataGolf key** behind the
   proxy pool. A D1 outage stalls fulfilment; DataGolf rate-limits/rotation stall that
   feed. Mitigations: the MCP's 7-day entitlement cache absorbs short Worker outages;
   grow the key pool before scaling DataGolf volume.
3. **Scraped/undocumented book endpoints drift (MEDIUM, ongoing).** Several providers hit
   unofficial book APIs (persisted GraphQL hashes, CDN routes) that change without
   notice. The OTA overlay + refresh-hashes tooling exist precisely to patch this in the
   field, but it's an operational treadmill, not a one-time fix.
4. **Chat-exposed secrets during this build (LOW, already flagged).** Over the project a
   Cloudflare API token, an R2 key, and a live licence key were pasted into chat. **All
   should be treated as compromised and rotated** if not already: revoke the `cfat_`
   token + R2 credentials at Cloudflare, and reissue the `sd_live_` licence.
5. **GitHub Actions billing/quota (LOW, operational).** Exhausted Actions minutes make CI
   release jobs fail in seconds — diagnosed earlier as billing, not code. Blocks the
   Windows CI release path until topped up.

### 7.4 Overall verdict
**Safe to run in production as-is** for the current single-customer / early-launch model.
The cryptographic boundaries (cost, operator, commerce integrity) are hard and
well-implemented. The must-fix-**before-scaling** items are: (a) code-signing both
desktop builds, (b) the managed-mode priced-model allow-list *if* that tier ships,
(c) rotating any chat-exposed credentials, and (d) reducing the D1 / single-DataGolf-key
single points of failure.

---

## 8. Production-readiness — what's left to launch broadly

**Already live / done:** commerce backend deployed, custom domain (`sportsdata-ai.com`)
connected, one real (test) purchase verified end-to-end, hashed D1 keys, download tokens,
kid rotation, OTA overlay, macOS `.app` + Windows build job, SECURITY.md + runbooks.

**Blocking broad launch (owner actions — external to code):**
1. **Apple Developer cert** → notarize the macOS app (removes the Gatekeeper scare).
2. **Windows Authenticode cert** → sign the Windows build (removes SmartScreen).
3. **Top up GitHub Actions** billing → re-run the Windows CI release.
4. **Tick "Enforce HTTPS"** on the `sportsdata-site` Pages settings.
5. **Rotate** the chat-exposed Cloudflare / R2 / licence credentials.
6. **A real (non-$0) test purchase** on a live card to prove the paid path.

**Engineering polish before scale (in the code):**
- Grow the DataGolf key pool; add a fallback/health-check on the `/proxy` path.
- Finish the R2 download path (scaffolded) as a CDN fallback to the GitHub release.
- Managed-mode priced-model allow-list *before* any non-BYO tier ships.
- Broaden MCP test coverage (6 modules for 492 tools is thin — add spec-contract tests
  that catch endpoint drift early).

---

## 9. Go-forward plan

**Now (finish the launch surface):** the remaining workbench PRs — B3 (per-agent model),
B6 (marketplace storefront → checkout handoff, no charging logic), B2 (per-conversation
model + tool scope, the deepest), then B7 (final cross-repo audit). None is blocked on
anything external. Recommended order: **B3 → B6 → B2 → B7**.

**Near term (harden the money path):** code-sign both builds, real paid purchase,
credential rotation, DataGolf pool + proxy health, R2 fallback.

**Mid term (durability & trust):** spec-contract test suite + drift alerting on scraped
books (turn the OTA treadmill into a monitored pipeline); status page / uptime for the
Worker; a lightweight admin view over D1 for support.

**Long term (growth):** decide if/when a **managed tier** (operator's keys) ships — and
gate it behind the priced-model allow-list; multi-seat / team licences (D1 already keys by
customer); expand provider coverage where it's cheap (new YAML specs), prune scraped
sources that drift too often.

---

_This document is a point-in-time review. When an enforcement seam, provider spec, or the
commerce flow changes, update the matching section here and in `docs/SECURITY.md`._
