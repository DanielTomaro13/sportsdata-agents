# P4 replanned: the desktop pivot

**The decision under consideration (Daniel, 2026-06-12):** instead of hosting a
multi-tenant web app, ship a **downloadable desktop app** — the agent harness
runs on the user's machine (their compute, storage, terminal), the way Cursor
is a harness around a local workspace. A web "app" survives only as a thin
chat UI. No central hosting of user data.

**Verdict up front: this is a simplification, not a rewrite.** The platform was
accidentally-on-purpose built local-first: the data plane is a stdio
subprocess, the warehouse is SQLite on local disk, the conductor is a local
loop, ops state lives in `~/.sportsdata-agents`, the custodian is already
disk-aware *because machines vary*, and agents already write to the local
filesystem (specs, skills, backups). Old-P4's hard parts — multi-tenant
isolation, hosted Postgres, metered billing infra — mostly **dissolve**. The
new hard parts are packaging, first-run UX, secrets, and auto-update.

---

## 1. The product shape (the Cursor analogy, made precise)

Cursor = a desktop shell + an agent harness + your local workspace + your model
key/subscription. Mapped onto sportsdata:

| Cursor | sportsdata desktop |
|---|---|
| The editor window | The **chat UI** (conversations, live tool-call feed, boards) |
| The repo you open | The **desk folder** the user picks (exports, models, reports land there) |
| Background indexing | The **conductor** (ingest/resolve/monitor/custodian) running as the app's daemon |
| Terminal & file tools | `run_python` sandbox, file export tools, the local warehouse |
| BYO API key / subscription | BYO model key in the OS keychain (subscription later) |
| Auto-update | Shell auto-updater + data-plane spec/dictionary OTA (D27 versioning already supports pins) |

One sentence: **the app is a supervised process tree** — UI shell → local
gateway (chat API + SSE) → agent runtime → MCP data-plane subprocess → SQLite
warehouse — all on the user's machine, with the conductor as its heartbeat.

## 2. What the user gets that a web app could never give them

- **Their own capture, their own IP** — each user's odds capture runs from
  their machine against books they can legally access. No central scraping
  service, no shared rate-limit pool, dramatically smaller ToS/compliance
  surface for us (relevant to the D13 legal review).
- **Their disk** — the full odds history is theirs; the custodian already
  adapts retention to whatever disk they have.
- **Their compute** — backtests, model fits and `run_python` analysis use
  their CPU at no marginal cost to us.
- **Their files** — agents export boards/backtests/reports straight into a
  folder they chose; the agent-builder already saves custom agents locally.
- **Privacy by architecture** — bets, models, bankroll notes never leave the
  machine. The only egress is model-API calls and the user's own Slack/Discord.

And what *we* get: no hosted warehouse, no per-tenant data isolation problem,
no 24/7 ingest fleet, no liability for holding other people's betting data.

## 3. Shell options considered

| Option | What it is | Pros | Cons |
|---|---|---|---|
| **A. Tauri shell + Python sidecar** ★ | Rust/system-webview shell; our stack ships as a bundled Python runtime the shell supervises | Small installers, native tray/menubar, first-class auto-updater + signing story, sidecar lifecycle built in | A thin Rust layer to own; Python bundling still ours to solve |
| B. Electron + sidecar | Same, Chromium shell | Most mature ecosystem | 150MB+ runtime, RAM-hungry, dated feel |
| C. Pure-Python shell (pywebview/briefcase) | One language end-to-end | No second toolchain | Weak auto-update/tray/signing ergonomics; least polished result |
| D. **Headless daemon + browser UI** ("the Ollama model") ★ | Installer puts a daemon + menubar item on the machine; the chat UI is served at `localhost` (and the hosted site can attach to it) | **Ships from current code fastest** — the gateway+SSE already exist; zero shell toolchain | "Open your browser" is less app-like; tray/autostart still needed per-OS |

**Recommendation: D first, A second — as phases, not a fork.** Build the chat
UI once; it serves from the local daemon on day one (D) and gets wrapped by
the Tauri shell later (A) with zero rework. This sequences all the risky
novel work (packaging, signing, updater) *after* the product is already
usable end-to-end on a user machine.

## 4. Architecture in detail

### 4.1 Process model
- New `agents app` supervisor: one process that runs (a) the gateway
  (uvicorn, localhost-only by default), (b) **the conductor as an in-process
  loop** (`run_tick` every 60s — cron/launchd disappears from user machines
  entirely), (c) health supervision of the MCP subprocess(es). Crash of any
  child → restart with backoff; failures still hand off to incident_triage.
- The existing `agents schedule --cron 60` crontab remains a server/dev
  deployment mode — same registry, two drivers.

### 4.2 Storage (OS-conventional, migration built-in)
- macOS `~/Library/Application Support/sportsdata/`, Windows `%APPDATA%`,
  Linux `~/.local/share/sportsdata/`: `warehouse.db`, `backups/`, `specs/`,
  `skills/`, `logs/`, `ops_state.json`, `exports/`.
- A user-chosen **desk folder** for agent outputs (reports, CSV/parquet
  exports, notebooks) — the Cursor-workspace equivalent. New small tool
  surface: `export_csv` / `write_report` scoped to that folder only.
- The `/tmp` saga ends permanently; the custodian's disk-aware ladder was
  built for exactly this heterogeneity.

### 4.3 Secrets
- Model keys (and TAB/Betfair/X creds) move from `.env` to the **OS keychain**
  (`keyring`), with `.env` kept as the dev/server fallback. First-run wizard:
  pick provider → paste key → live test call → stored in keychain. Keys never
  sit in a file on user machines.

### 4.4 The chat UI (the "web app that is just chat")
- One SPA, two mounts: bundled into the app AND attachable from the hosted
  marketing site to a detected local daemon (CORS allowlist + a pairing token
  the daemon prints/displays — never an open localhost API).
- Built on what exists: `POST /conversations/{id}/message`, async tasks, and
  `GET /tasks/{id}/events` SSE — the **live tool-call feed is the demo
  console grown up** (the site's animated playback was the prototype).
- Panels beyond chat (all backed by existing endpoints/tools): alerts feed
  (arb/value with outcome stamps), watch management, schedule/status board,
  cross-book fixture boards, custodian/disk status.
- Auth posture: localhost binding + pairing token; `--demo-only` stays the
  only internet-facing mode.

### 4.5 Updates & the release pipeline
- Shell: Tauri updater (signed). Sidecar: versioned with each release
  (python-build-standalone + onedir layout; avoid one-file PyInstaller's slow
  cold starts).
- Data ships as data: market dictionary seeds, capability labels and spec
  catalogues can update OTA between releases — D27 pinning means user
  workspaces never break.
- The self-improvement loop becomes the release train: ops agents → PR → CI →
  tagged release → updater. The reviewer-bot account (POST_DEV) graduates
  from nice-to-have to required.

### 4.6 Monetization & the tiny remaining cloud
- **License key + BYO model key** first (Paddle/LemonSqueezy handle VAT for
  desktop software; Stripe fine too). A ~50-line hosted license endpoint is
  the only mandatory server.
- Optional later: subscription with bundled model quota (we proxy model
  calls — that's the one feature that genuinely needs a server), mobile alert
  relay, hosted spec feed. All opt-in additions, none structural.
- Old-P4 items disposition: multi-tenancy → each install is `tenant=local`
  (the seam stays for a future cloud tier); auth → license + pairing token;
  billing → licensing; hosted Postgres → gone (server deployments can still
  use it; `migrate-warehouse` already exists).

## 5. Honest trade-offs (the cons, stated plainly)

1. **Support surface explodes** — every user's machine is a unique snowflake.
   Mitigations: the ops plane ships IN the product (health checks, custodian,
   triage run on the user's box), plus opt-in crash/telemetry reporting.
2. **No central data moat** — we don't accumulate one giant cross-user odds
   history. Counterpoint: we keep our own reference capture, and per-user
   capture is what makes the legal posture clean.
3. **Slower iteration than a web deploy** — releases, not pushes. Mitigated by
   data-as-updates and the OTA spec feed.
4. **Piracy/licensing** is a thing desktop apps live with. Accept it; BYO-key
   means pirates cost us nothing in model spend.
5. **Signing/notarization friction** — Apple dev account, Windows cert,
   notarize in CI. Known, bounded, annoying.
6. **Books may dislike distributed capture** — same scraping questions as
   today but multiplied by users; needs a line in the D13 legal review and
   respectful client behaviour (the pacing/rotation discipline already
   exists).

## 6. Revised P4 milestones

- **M4.1 — Daemonize (pure Python, no shell):** `agents app` supervisor
  (gateway + conductor loop + MCP supervision in one process); OS-conventional
  storage layout with automatic migration from current paths; keychain
  secrets with `.env` fallback; first-run wizard (CLI flavour). *Exit gate:
  fresh machine → `pipx install` → wizard → capturing, chatting, alerting
  with NO crontab and NO .env.*
- **M4.2 — The chat UI:** SPA over the existing gateway (conversations, SSE
  tool feed, alerts/watches/status/boards panels); pairing token + CORS;
  served from the daemon. *Exit gate: a full desk session — ask for arbs,
  watch the tool feed stream, manage a watch, export a board to the desk
  folder — entirely from the browser UI.*
  **Delivered:** chat UI shipped (v0.16.0); the **desk folder + export tools**
  shipped (v0.18.0) — `export_csv`/`write_report` (any agent, scoped to the
  desk folder, traversal-safe) and `export_training_data` (the DB→file bridge
  so the modelling sandbox can read captured price history). `agents desk
  [--set]` + the setup wizard let the user pick where exports land.
- **M4.3 — Package & sign (macOS arm64 first):** Tauri shell wrapping M4.2,
  sidecar bundling (python-build-standalone), menubar + start-at-login, DMG +
  notarization, auto-updater, brew cask. *Exit gate: a signed DMG installs on
  a clean Mac and survives an auto-update cycle.*
- **M4.4 — License & distribute:** license endpoint + key entry in the wizard;
  download page on the marketing site (replacing "private beta" copy); opt-in
  telemetry/crash reports. *Exit gate: a stranger can pay, download, install,
  and run without us in the loop.*
- **M4.5 — Windows, then Linux; OTA data feed:** second/third platforms;
  spec/dictionary update channel. *Exit gate: same wizard-to-alert flow on
  Windows.*

Sequencing note: M4.1+M4.2 are pure Python on the existing codebase and
deliver a usable "downloadable" product (pipx/installer script) before any
shell/signing work begins. M4.3+ is where the new toolchain risk lives.

## 7. What actually changes in current code (surprisingly little)

| Area | Change |
|---|---|
| Conductor | add `--loop` driver around the existing `run_tick` (registry untouched) |
| Config | per-OS default paths + one-time migration; keyring-backed secrets with env fallback |
| Gateway | pairing-token middleware + CORS allowlist (demo-only gate already exists) |
| New | `agents app` supervisor; desk-folder export tools; the SPA |
| Untouched | feeds, normalizers, resolver, quant, monitor, custodian, ops plane, specs, evals — the entire engine |

## 8. Open questions for Daniel before M4.1 starts

1. **Platform order** — macOS arm64 first (your machine), then Windows?
2. **Key model** — launch BYO-key only, or build the bundled-quota
   subscription (needs a model-proxy server) into v1?
3. **Name/brand** for the app itself (the site says "sportsdata").
4. **Hosted chat-attach** — is "the marketing site can talk to your local
   daemon" worth its security review in v1, or is bundled-UI-only enough?
5. **Slack/Discord** stay first-class alongside the app UI (they already work
   from a local daemon) — confirm that's the intent.
