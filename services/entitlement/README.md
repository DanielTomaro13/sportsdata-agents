# sportsdata entitlement service (Phase 1)

The small always-on piece of the commerce stack (see `../../docs/COMMERCE_PLAN.md`).
A Cloudflare Worker + D1 that:

- takes the **Stripe webhook** → stores each customer's entitlement (slot counts +
  status) and issues a **licence key**,
- serves **`GET /entitlement`** → a **signed** grants token the MCP verifies offline,
- (Phase 1b) **proxies the credentialed feeds** (DataGolf + TAB) so our keys never
  ship in self-host builds.

Stripe tracks *how many* slots; this service tracks the licence; the **licence is the
feed list** the MCP serves — so buying a feed needs no client config change.

## Endpoints
| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| POST | `/stripe/webhook` | Stripe signature | subscription events → entitlement |
| GET | `/entitlement` | `Authorization: Bearer <licence key>` | signed grants token |
| GET | `/proxy/<provider>/<path…>` | `Authorization: Bearer <licence key>` | credentialed-feed proxy (Phase 1b) |
| GET | `/healthz` | — | liveness |

### Credentialed-feed proxy (Phase 1b)

Some feeds run on **our** upstream credentials and must never ship inside a self-host
build. A licensed MCP points those providers at `GET /proxy/<provider>/<upstream-path>`
with the licence key as `Authorization: Bearer`; the Worker checks the licence grants the
provider, attaches the credential **server-side**, and streams the response back.

- **`datagolf`** — attaches our paid `?key=` (`DATAGOLF_KEY` secret). The only provider
  that *requires* the proxy today.
- **`tab`** — wired for the OAuth case (mints a `client_credentials` token from
  `TAB_CLIENT_ID/SECRET`), but **inert unless those secrets are set**. TAB's public
  endpoints need no auth and TAB geo/bot-manages by IP, so they are better run
  client-side from the customer's own IP.

The proxy is GET-only, pins the upstream host (no SSRF via the path), and never forwards
`Set-Cookie`. A provider with no configured credential returns `503` (inert), and a
licence that doesn't grant the provider returns `403`.

## Deploy (you run this — secrets stay in your shell / Cloudflare)

```sh
cd services/entitlement
npm install

# 1. Create the D1 database, paste the printed database_id into wrangler.jsonc
npx wrangler d1 create sportsdata-entitlement

# 2. Apply the schema (local + remote)
npx wrangler d1 execute sportsdata-entitlement --file=schema.sql --remote

# 3. Generate the signing keypair (needs Python + `cryptography`)
python gen-keypair.py
#   → set the PRIVATE line as the Worker secret; keep the PUBLIC line for the MCP
npx wrangler secret put SIGNING_KEY_PKCS8_B64     # paste the private (PKCS8 b64)

# 4. Other secrets
npx wrangler secret put STRIPE_SECRET_KEY         # your Stripe secret key
npx wrangler secret put STRIPE_WEBHOOK_SECRET     # filled in step 6 (whsec_…)

# 5. Deploy → note the workers.dev URL
npx wrangler deploy

# 6. Stripe → Developers → Webhooks → Add endpoint:
#      URL    = https://<your-worker>.workers.dev/stripe/webhook
#      events = customer.subscription.created / .updated / .deleted
#    Copy the endpoint's signing secret (whsec_…) and re-run step 4 for it.
```

Test with the Stripe CLI: `stripe trigger customer.subscription.created`, then
`GET /entitlement` with the new licence key (find it in D1, see below).

## Fulfilment (Phase 0, manual)
Until Phase 5 automates the email, find a new customer's licence key:
```sh
npx wrangler d1 execute sportsdata-entitlement --remote \
  --command "SELECT id, email, stripe_customer_id FROM customers ORDER BY created_at DESC LIMIT 10"
```
Send them that `id` (the `sd_live_…` licence key) + the MCP setup.

## Next
- **Phase 2** ✅ — the MCP licence gate verifies this service's token and serves only the
  granted groups (`sportsdata-mcp` v0.9.0). Bake the `gen-keypair.py` public key before
  shipping a licensed build.
- **Phase 4** — feed assignment (`groups`) + in-app add-on purchase.
- **Phase 5** — fulfilment automation (webhook → issue key → email the licence + config).
