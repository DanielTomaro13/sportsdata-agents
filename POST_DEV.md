# Post-dev checklist — everything that can be turned on later

Every item here is **fully built and tested**; nothing blocks development. Each
needs only an account, a credential, a host, or a single command — do them in
any order, whenever. (Anything requiring actual code lives in
[`BUILD_PLAN.md`](./BUILD_PLAN.md), not here.)

## Accounts & credentials (create once, paste into `.env`, done)

- [ ] **GoatCounter analytics** — the site ships a no-cookie analytics loader that
  is OFF (`window.ANALYTICS_URL = null` in `site/index.html`). Turn on: create a
  free site at goatcounter.com, set `window.ANALYTICS_URL =
  "https://YOURCODE.goatcounter.com/count"`, run `scripts/deploy-site.sh`.
  Real page views then flow into `site_traffic`'s weekly report.
- [ ] **Discord pushes** — alerts/ops reports are Slack-live, Discord-ready.
  Turn on: create a channel webhook (Channel settings → Integrations →
  Webhooks), set `DISCORD_WEBHOOK_URL` (alerts) and/or `OPS_DISCORD_WEBHOOK`
  (ops reports + escalations) in `.env`. Point any watch at Discord with
  `channel="discord"` (or `discord:MY_ENV_VAR` for a dedicated webhook).
- [ ] **Discord chat bot** — `agents discord` is built (mention/DM → the team).
  Turn on: create a bot in the Discord developer portal (message-content
  intent), set `DISCORD_BOT_TOKEN`, `pip install -e ".[discord]"`, run
  `agents discord` against a running gateway.
- [ ] **X (Twitter) bearer token** — the 15 social tools and the news_scout agent
  are wired but keyless. Turn on: create an X API project, set
  `TWITTER_BEARER_TOKEN` in `.env` (the MCP subprocess inherits it).
- [ ] **Reviewer-bot GitHub account** — GitHub blocks self-approval, so
  code_reviewer comments instead of approving its own PRs. Turn on: create a
  second GitHub account with repo access, set its token as `OPS_GITHUB_TOKEN`;
  the review flow needs no code change.
- [ ] **Betfair authed key** — fetcher + normalizer exist; the public readonly
  key returns no price sections from AU. Turn on: a funded Betfair app key in
  `.env`, then re-register the feed (see BUILD_PLAN P4 notes).
- [ ] **Rotate keys pasted in chat (2026-06-10)** — Anthropic, Groq, Gemini,
  OpenRouter, TAB client id/secret, Slack tokens. Standing hygiene item.

## Infrastructure (host something, flip a switch)

- [ ] **Postgres/Timescale move** — retires the deliberate `/tmp` warehouse risk.
  Turn on: provision Postgres (Timescale optional — migration 0009 guards
  itself), pause ingest (`crontab -e`, comment the conductor line), run
  `agents migrate-warehouse <postgres-url>` (FK-ordered, idempotent, resumable
  with `--allow-nonempty`), point `SPORTSDATA_AGENTS_DATABASE_URL` at it,
  un-comment the conductor. The mover was dry-run-verified on 864k rows.
- [ ] **Hosted gateway + live demo flip** — the site is playback-only
  (`window.GATEWAY_URL = null`). Turn on: host `agents serve` somewhere
  public, set `window.GATEWAY_URL` in `site/index.html`, redeploy. Before
  flipping, do the one small dev item attached to this: bump the demo budget
  (12 tool calls/16 steps truncates compare-books) and the two "Slack" →
  "Slack or Discord" copy lines in `site/demo-fallback.json`.
- [ ] **Polymarket feed** — built, tested, ops-disabled because the Gamma edge
  geo-blocks AU. Turn on (from any non-blocked host): remove
  `polymarket_all` from `disabled_feeds` in `~/.sportsdata-agents/ops_state.json`
  (or have incident_triage re-enable it); the next conductor tick captures.

## Switches already wired (one command, no accounts)

- [ ] **Line-move / steam watches** — switched off by choice (alert volume); the
  arb watch stays on. Turn on: `agents monitor --add "name:line_move:8"
  --channel C...` or reactivate the existing subscriptions (`active=1`).
  Digest mode (`params.digest_hours`) tames the volume that got them muted.
- [ ] **Demo re-record** — `scripts/record-demo.py` replaces the curated
  fallback with real gateway runs (review the diff; raw replays read rougher
  than the curated copy — that's why it's optional).
- [ ] **More Kalshi league aliases** — 42 GAME series are seeded; when Kalshi
  lists a new league, one steward alias (`kx<league>game` → `h2h`) joins it to
  cross-book boards. The Monday steward run surfaces unseeded ones by itself.

*Everything on this list is safe to ignore until needed — the platform runs
without any of it.*
