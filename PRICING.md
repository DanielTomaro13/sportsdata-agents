# sportsdata — tiers & pricing

Three products on one engine, gated by an offline-verified license key
(`src/sportsdata_agents/licensing/`). Every tier is **BYO model key** — you pay
for your own LLM usage directly to Anthropic/OpenAI/etc; our subscription is for
the software, data plane, and agents. Prices are launch suggestions in **USD/mo**
(annual ≈ 10× monthly); finalise against actual model/data costs before charging.

| | **Base** | **Plus** | **Pro** |
|---|---|---|---|
| **What it is** | The data plane + a setup client — wire the MCPs into your own MCP client (Claude Desktop, Cursor, …) | Adds the chat interface to the agent team | The full desktop app — the whole agent team + the always-on conductor |
| **MCP provider groups** | 5 included | 12 included | unlimited |
| **Agents** | — (raw tools) | core team (orchestrator, odds, stats, concierge) | every agent (modelling, value, arb, fantasy, news, …) |
| **Odds warehouse + conductor** | — | — | ✓ (ingest/resolve/monitor/custodian) |
| **Standing alerts (arb/value/line)** | — | — | ✓ |
| **Suggested price** | **$15/mo** | **$39/mo** | **$89/mo** |

### Add-ons (paid toggles on any qualifying tier)

| Add-on | What | Suggested price |
|---|---|---|
| **Slack integration** | chat + alerts in Slack (Pro) | $9/mo |
| **Discord integration** | chat + alerts in Discord (Pro) | $9/mo |
| **Extra MCP pack** | +5 included provider groups (Base/Plus) | $8/mo each |
| **Premium data** | unlock DataGolf / authed Betfair once you supply the key | $12/mo |

### Free tier (no license)

Two MCPs, no chat UI, no daemon — enough to evaluate the data plane in your own
client. A tampered, expired, or missing license always falls back to this, never
to a locked-out state.

## How the gating works (for us, not the customer)

- **License = Ed25519-signed JSON.** The public key ships in the binary; the
  private key issues licenses (a payment-webhook secret, never in the app), so
  the app verifies OFFLINE — no phone-home, works on a plane.
- Entitlements are enforced at the existing seams: the **team roster** is
  filtered to the tier's agents, the **MCP group list** is capped to the quota,
  and **Slack/Discord/app** commands check their gate. Ops-plane agents are never
  gated — they're our maintenance crew, not a feature.
- `agents license` shows the active tier; `agents license --activate <key>`
  stores a key in the OS keychain.

## What's needed to actually charge (POST_DEV)

1. **Generate the signing keypair** once (`scripts/license.py keygen`), bake the
   public key into the build (`SPORTSDATA_LICENSE_PUBKEY`), keep the private key
   in the payment webhook only.
2. **Payment processor** — Paddle or LemonSqueezy (they handle VAT/GST for
   downloadable software; merchant-of-record removes our tax burden). On a
   successful charge, the webhook calls `issue_license(...)` and emails the key.
3. **Apple Developer ID** ($99/yr) to sign + notarize the Mac build so Gatekeeper
   doesn't warn on a direct (non-App-Store) download. Not App-Store review — just
   the Developer ID cert + `notarytool`.
