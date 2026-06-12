# Next steps

Status as of v0.30.0. The platform is **code-complete** through Phase 4 (the
downloadable desktop app), including the generalist growth loop and the operator
console (CLI + the in-app operator panel). What remains is account setup and a
small set of deliberately-deferred items.

## To go live (account/setup — no code)

1. **Licence keypair** — `python scripts/license.py keygen`; bake the public half
   into the build env (`SPORTSDATA_LICENSE_PUBKEY`). 5 minutes.
2. **Payments + delivery** — a Paddle or LemonSqueezy account + products; set
   `SPORTSDATA_BILLING_PRODUCTS`, the webhook secret, and SMTP creds; point the
   webhook at `agents billing`. The webhook code is done. **Mint short tokens for
   monthly plans** (see the billing decision below).
3. **Apple Developer ID** ($99/yr) — enrol, export the Developer ID cert, add the
   repo secrets ([../RELEASE.md](../RELEASE.md)), tag a release.
4. **Public download host** — the agents repo is private, so attach the signed DMG
   to a Release in the **public** `sportsdata-site` repo (the download button points
   there). A cross-repo publish step (PAT) would make this turnkey.

## Subscription model (decided)

Monthly plans mint **short tokens** (`days: 33` in the product map); each renewal
re-issues, and a cancelled subscriber keeps access until the paid period ends.
Renewal pickup is `agents license --refresh` against the billing app's
`POST /licence/refresh` (present your current token, get the latest one issued to
you — it can never extend access beyond what renewals minted). Details in
[../POST_DEV.md](../POST_DEV.md).

## Deferred / future (code, not blocking)

- **In-app auto-updater** (Sparkle) + Homebrew cask — needs a stable download URL.
- **Windows, then Linux** packaging — reuses the same `agents app` daemon; needs the
  per-OS shell/installer + an Authenticode cert (Windows).
- **Tauri native shell** — a native window/menubar replacing the browser (same
  sidecar). Pure upgrade over the current "daemon + browser UI" model; needs Rust.
- **Bundled-quota subscription** — proxy model calls so users don't BYO key. The one
  feature that genuinely needs a server.
- **OTA: book catalogue** — fold `CATALOGUE.json` into the data feed (it's currently
  a read-only package path; also fixes `refresh-books` writing into the package).
- **Per-seat enforcement** — claims-only today (fine for single-user v1).
- **Opt-in telemetry / crash reports** — the "every machine is a snowflake" support
  mitigation; needs a destination endpoint.

## Health of the codebase

520+ tests (unit + integration), green CI gates, ~37/60 data capabilities leveraged
with a coverage guard. The self-improvement loop (ops agents → CI-gated PRs) is live,
and the platform grows per-user via the generalist's skill library. This file and the
rest of `docs/` are kept current by the **docs_keeper** ops agent.
