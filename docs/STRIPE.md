# Selling the Base plan with Stripe

The marketing site (`site/index.html`, published to the public `sportsdata-site`
Pages repo) is a **static page with no backend**. So checkout uses Stripe-hosted
**Payment Links** — no server to run, and no card data ever touches our code.

Current launch posture: the **app is "coming soon"** (Plus/Pro shown but not
purchasable). Only **Base** sells, plus add-ons:

| SKU | What | Price |
| --- | --- | --- |
| `base` | Data plane — 5 sport data MCPs of your choice, wired into your own AI client | **$15/mo** |
| `sport_addon` | One extra sport data MCP beyond your first 5 (adjustable qty) | **+$5/mo** |
| `gambling_addon` | One live bookmaker / odds MCP (adjustable qty) | **+$15/mo** |
| `all_access` | Every sport **and** gambling MCP | **$99/mo** |

Edit these in `scripts/setup-stripe.py` (the `CATALOGUE` dict) if you want different
numbers.

## Go live (you run this — your key never leaves your shell)

1. Install the SDK once: `pip install stripe`
2. **Test mode first** (fake cards, no real money):
   ```sh
   export STRIPE_SECRET_KEY=sk_test_xxx
   python scripts/setup-stripe.py        # creates products + Payment Links, writes site/stripe.json
   ```
   Open `site/index.html`, click **Get access →**, pay with Stripe's test card
   `4242 4242 4242 4242` (any future expiry / any CVC). Confirm the subscription
   appears in your Stripe **test** dashboard.
3. **Live**, when you're happy — set your live secret key in *your own* shell (never
   in the repo, never in chat), then re-run the same script:
   ```sh
   export STRIPE_SECRET_KEY=sk_live_xxx   # the script will ask you to confirm 'live'
   python scripts/setup-stripe.py
   sh scripts/deploy-site.sh              # publishes the site + stripe.json
   ```

`site/stripe.json` (the Payment Link URLs) is **gitignored** — it's generated, and
test vs live links differ. The site fetches it at runtime; until it exists the Base
button shows a friendly "being set up" message instead of dead-linking.

## Fulfilment (the open piece)

A Payment Link charges the customer; it does **not** yet provision their MCP access.
For launch you can fulfil **manually**: Stripe emails you on each new subscription →
you issue them a licence key (`agents` licensing) + send the MCP config for their
chosen feeds. Automating this (Stripe webhook → licence issue → email) is the next
step — it needs a small always-on endpoint (the Payment Links themselves don't).

## Security

- The **secret key** (`sk_live_…` / `sk_test_…`) is read from `STRIPE_SECRET_KEY`
  in your environment and is never stored, printed, or committed. Set the live key
  only in your host's secret store / your own shell.
- The **publishable key** (`pk_…`) is safe to expose — but note Payment Links don't
  even need it on the page, so it isn't embedded.
- If a secret key is ever exposed (e.g. pasted somewhere), roll it in the Stripe
  dashboard → Developers → API keys.
