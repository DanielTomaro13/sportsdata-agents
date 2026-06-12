# Post-dev checklist — everything that can be turned on later

Every item here is **fully built and tested**; nothing blocks development. Each
needs only an account, a credential, a host, or a single command — do them in
any order, whenever. (Anything requiring actual code lives in
[`BUILD_PLAN.md`](docs/history/BUILD_PLAN.md), not here.)

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

## Going commercial (P4 — the three account-gated steps to charge money)

The entitlement/licensing code is DONE and tested (`src/sportsdata_agents/licensing/`,
`PRICING.md`). These three need your accounts/money to switch from "code-complete"
to "taking payments":

> **✓ Subscription expiry (DECIDED, Daniel 2026-06-13).** Monthly subscriptions mint
> **short tokens** — set `days: 33` in `SPORTSDATA_BILLING_PRODUCTS` for monthly
> products (annual plans can use 370). Each renewal webhook re-issues a fresh token;
> a **cancelled subscriber keeps access until their paid period ends** (the token
> simply expires with it) — accepted behaviour, no revocation needed. Renewal pickup
> is frictionless: the billing app exposes **`POST /licence/refresh`** (present the
> current — even just-expired — token, get back the latest one issued to the same
> buyer; it can never extend access, only deliver what renewals already minted), and
> customers run **`agents license --refresh`** (`SPORTSDATA_LICENSE_REFRESH_URL`
> points at the endpoint, baked into product builds). Nothing left to decide here.

- [ ] **Generate the license signing keypair** — `python scripts/license.py keygen`
  (run once). Put the PUBLIC key in the build env (`SPORTSDATA_LICENSE_PUBKEY`) so
  shipped builds enforce; keep the PRIVATE key in the payment webhook secret only.
  Until this is done every build runs unrestricted (source) / free-tier (if a
  pubkey is set) — no one is gated yet, by design.
- [ ] **Payment processor** — Paddle or LemonSqueezy (merchant-of-record: they
  handle GST/VAT for downloadable software so you don't). **The webhook server is
  CODE-COMPLETE and tested** — `src/sportsdata_agents/licensing/billing.py` +
  `agents billing` (provider-agnostic: a thin adapter per processor verifies the
  signature and extracts the purchase; the core maps product→tier and calls
  `issue_license`). What's left is account-side, not code:
  1. Create a Paddle/LemonSqueezy account, add the product/price for each tier+add-on.
  2. Set env on the host running `agents billing`: `SPORTSDATA_LICENSE_PRIVKEY`
     (the signing key from keygen), `SPORTSDATA_BILLING_PRODUCTS` (JSON mapping
     `{provider: {product_id: {tier, addons, days}}}`), and the webhook secret
     (`PADDLE_WEBHOOK_SECRET` / `LEMONSQUEEZY_WEBHOOK_SECRET`).
  3. Point the processor's webhook at `https://…/webhook/paddle` (or
     `/webhook/lemonsqueezy`) behind a TLS proxy. Signature + replay window are
     enforced; bad signatures 401, unmapped products 400.
  4. **Delivery**: the issued key is always journaled to `issued-licenses.jsonl`,
     and **emailed automatically when SMTP is configured** (`deliver_license` /
     `send_license_email`). Set `SMTP_HOST` (+ `SMTP_PORT`/`SMTP_USER`/
     `SMTP_PASSWORD`/`BILLING_FROM_EMAIL`, STARTTLS on by default) on the host
     running `agents billing` and the buyer gets their key with `agents license
     --activate <key>` instructions; a send failure falls back to the audit log
     (never 500s the webhook). With no SMTP set you just email keys from the log
     manually. No remaining code TODO — this step is purely account/credential setup.
  This ~one small server is the only server the desktop model needs.
- [ ] **Apple Developer ID** ($99/yr) — sign + notarize the Mac build so Gatekeeper
  doesn't warn on a direct download (NOT App Store review — just `codesign` with the
  Developer ID cert + `notarytool`). **The whole pipeline is built and waiting** —
  `scripts/build-desktop.sh` → `scripts/make-macos-app.sh` → `scripts/sign-and-notarize.sh`,
  plus a tag-triggered `.github/workflows/release.yml`. Just enrol, export the
  Developer ID cert, add the repo secrets, and tag a release. **Full runbook:
  `RELEASE.md`.** No remaining code — purely account setup. Windows later wants an
  Authenticode cert (same `agents app` daemon).

## Infrastructure (host something, flip a switch)

- [ ] **Postgres/Timescale move** — retires the deliberate `/tmp` warehouse risk.
  Turn on: provision Postgres (Timescale optional — migration 0009 guards
  itself), pause ingest (`crontab -e`, comment the conductor line), run
  `agents migrate-warehouse <postgres-url>` (FK-ordered, idempotent, resumable
  with `--allow-nonempty`), point `SPORTSDATA_AGENTS_DATABASE_URL` at it,
  un-comment the conductor. The mover was dry-run-verified on 864k rows.
- [ ] **Hosted gateway + live demo flip** — the site is playback-only
  (`window.GATEWAY_URL = null`). Turn on: host **`agents serve --demo-only`**
  (⚠️ the ONLY mode meant to face the internet — the full gateway is the
  localhost desktop daemon and would be an open model-spend endpoint if
  reverse-proxied), set `window.GATEWAY_URL` in `site/index.html`, redeploy. Before
  flipping, bump the demo budget (12 tool calls/16 steps truncates
  compare-books).
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
