# sportsdata — commerce & delivery plan

How we sell the **MCP data plane** (the app is "coming soon"), how a customer gets
set up, and how they **buy more feeds** later. Pairs with [`STRIPE.md`](STRIPE.md)
(the Stripe go-live runbook).

---

## 0. The one idea everything hangs on

> **Stripe tracks _how many_ feed slots a customer pays for.
> Our system tracks _which_ feeds they put in those slots.**

Stripe only ever knows counts — "5 base + 2 extra sport + 1 gambling". *Which*
specific feeds (AFL vs NBA, Sportsbet vs TAB) lives in our entitlement record and the
app. That separation is what lets someone buy "+1 gambling MCP" without Stripe needing
to know anything about our catalogue.

---

## 1. Pricing & SKUs

Delivery is **self-host** (the customer runs the MCP on their own machine), so our
infra cost is ~$0 and the scraped/gambling feeds run from the customer's own IP (far
less blocking). See §7 for the full cost picture.

| SKU (Stripe line item) | Price | Quantity means | Example |
| --- | --- | --- | --- |
| **Base** | $15/mo | always 1 | 5 sport slots included |
| **Extra sport MCP** | $5/mo | # extra sport feeds | qty 2 → 7 sport feeds |
| **Gambling MCP** | $15/mo | # odds feeds | qty 1 → 1 bookmaker feed |
| **All-access** | $99/mo | replaces the above | every feed |

One **subscription** per customer carries all the line items. "Buy more" = increment a
line-item quantity; Stripe prorates automatically. Downgrade = decrement.

> Numbers are the launch starting point — change them in `scripts/setup-stripe.py`.

---

## 2. Architecture — three pieces

```
   ┌─────────────┐    buy / upgrade    ┌──────────────────┐
   │  Customer   │ ─────────────────▶  │  Stripe          │
   │ (their AI   │                     │  (Checkout +     │
   │  client +   │ ◀──── receipt ───── │  Customer Portal)│
   │  the app)   │                     └────────┬─────────┘
   └──────┬──────┘                              │ webhook: subscription changed
          │ GET /entitlement?key=…              ▼
          │ ◀──── signed grants ────  ┌────────────────────────┐
          └──────────────────────▶    │  Entitlement service   │
                                      │  (Cloudflare Worker+D1)│
                                      │  key → slots + status  │
                                      └────────────────────────┘
```

### (A) Entitlement service — the only always-on infra (tiny & cheap)
A small Cloudflare Worker + D1 database. **Not** the data plane — just a key→grants
lookup.
- `POST /stripe/webhook` — on subscription create/update/cancel, recompute the
  customer's `sport_slots`, `gambling_slots`, `all_access`, `status`, `valid_until`.
- `GET /entitlement?key=…` — returns those grants, **signed** (so the app trusts them
  offline).
- Issues the **licence key** on first purchase, tied to the Stripe customer id.

### (B) Licence gate inside the MCP
On startup the MCP:
1. reads the local licence key, calls `/entitlement`, **caches** the signed response
   (≈7-day offline grace so a brief outage doesn't lock the customer out),
2. **verifies the signature** with a baked-in public key (reuses the existing
   operator-licence signing infra in `sportsdata-agents/licensing`),
3. registers **only** the tools for the groups the entitlement grants. Editing a local
   file can't grant more — the grants are signed.

### (C) The app (delivery)
The downloadable build (or `uvx` + a config snippet) with a **Feeds** screen that does
both **selection** and **purchasing** (§4).

---

## 3. Catalogue: sport vs gambling groups

The MCP exposes ~24 providers as groups. The store splits them:

- **Sport data** (count toward the 5 + sport add-ons): `afl.*`, `espn.*`, `nba.*`,
  `nrl.*`, `cricketaustralia.*`, `openf1.*`, `mlb.*`, `datagolf.*`, league feeds, …
- **Gambling / odds** (the $15 add-ons): `sportsbet.*`, `tab.*`, `betfair.*`,
  `pinnacle.*`, `pointsbet.*`, `entain.*`, `unibet.*`, `fanduel.*`, `kalshi.*`,
  `polymarket.*`.

The split is a tag on each provider (one small map), so the app can show two pickers.

---

## 4. Customer flows — step by step

### Initial purchase
1. Website → Stripe Checkout → pays **Base $15**.
2. Webhook → service creates the customer + entitlement (`sport_slots: 5`) and
   **issues a licence key** → fulfilment email (key + download link + setup steps).
3. Customer runs the app → pastes the key → **picks which 5 sport feeds** → the app
   self-registers into their AI client → restart → done.

### Adding a sport feed (their 6th) — the core "buy more" loop
1. In the app's **Feeds** screen they tap **"Add sport feed +$5/mo"**.
2. App opens a Stripe **Checkout / Customer Portal** link that increments the *Extra
   sport* quantity.
3. They confirm → Stripe charges the prorated amount → fires the webhook.
4. Service bumps `sport_slots: 6` → re-signs the entitlement.
5. App **refreshes** (button, or on next launch) → sees the free slot → lets them
   **assign** their 6th feed → re-registers → restart.

### Adding a gambling feed
Identical, using the *Gambling* line item (**+$15/mo**) and the catalogue filtered to
odds providers.

### Upgrade to All-access
One button → Checkout swaps the line items to the **$99** plan → webhook sets
`all_access: true` → every feed unlocks.

### Manage / cancel / update card
A **"Manage billing"** button opens the **Stripe Customer Portal** (Stripe-hosted,
zero build for us). Any change webhooks back and the entitlement re-syncs.

---

## 5. Enforcement (so it can't be taken for free)
- Entitlement responses are **signed by the service**; the app **verifies with a baked
  public key** — no local tampering grants extra feeds.
- **Offline:** cached grants honored for a grace window, then the MCP degrades to
  "licence check needed".
- The 5-vs-6 **assignment** is also stored server-side, so reinstalling can't reset
  picks or grab extra slots.
- The MCP repo stays **private**; distribution is a signed build / `uvx` from a
  controlled index, not public PyPI (decision pending — see §9).

---

## 6. Connecting the MCP to your AI tool

This is what the customer does once they have a licence key. The app **generates the
exact block for them** (pre-filled with their key + chosen feeds), but here's what it
is and how it works for each client. Every MCP client speaks the same pattern: run a
command (the MCP server) and pass two env vars —
`SPORTSDATA_MCP_GROUPS` (their feeds) and `SPORTSDATA_LICENSE` (their key).

> `command` is either `uvx` (if they installed via uvx) **or** the absolute path to
> the bundled binary inside the downloadable app — e.g.
> `/Applications/sportsdata-mcp.app/Contents/MacOS/sportsdata-mcp`. With the
> downloadable build the customer needs **no Python at all**.

### Claude Desktop
Config file:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "sportsdata": {
      "command": "uvx",
      "args": ["sportsdata-mcp"],
      "env": {
        "SPORTSDATA_MCP_GROUPS": "afl.public.core,espn.scores,nba.core,cricketaustralia.core,openf1.reference",
        "SPORTSDATA_LICENSE": "sd_live_xxxxxxxx"
      }
    }
  }
}
```
Then **fully quit and reopen Claude Desktop** → the sportsdata tools appear under the
tools (🔌) menu. (With the downloadable build, the app writes this file for them and
just says "restart Claude Desktop".)

### Cursor
Config file: `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (per-project). Same
shape:
```json
{
  "mcpServers": {
    "sportsdata": {
      "command": "uvx",
      "args": ["sportsdata-mcp"],
      "env": { "SPORTSDATA_MCP_GROUPS": "…", "SPORTSDATA_LICENSE": "sd_live_xxxxxxxx" }
    }
  }
}
```
Then **Cursor → Settings → MCP** → toggle **sportsdata** on. Tools show in the agent
panel.

### VS Code (Copilot agent / MCP)
`.vscode/mcp.json` (workspace) or the user `mcp.json`. VS Code nests under a `servers`
key:
```json
{
  "servers": {
    "sportsdata": {
      "command": "uvx",
      "args": ["sportsdata-mcp"],
      "env": { "SPORTSDATA_MCP_GROUPS": "…", "SPORTSDATA_LICENSE": "sd_live_xxxxxxxx" }
    }
  }
}
```
Then enable it from the MCP view.

### Other MCP clients (Cline, Windsurf, Zed, Continue, …)
All use the **same stdio pattern** — a `command` + `args` + `env`. Point `command` at
`uvx` (or the bundled binary) with the two env vars. Anything that speaks MCP works.

### Verifying it's connected
Ask the assistant something like *"using sportsdata, show today's NBA scoreboard"* — if
the feeds are wired, it calls the tool and answers from live data. If a feed isn't in
their plan, the tool simply isn't registered (the licence gate), so the assistant won't
see it.

### Hosted option (future, Option A)
If we later offer a hosted endpoint, the same clients point at a URL instead of a
command — `"url": "https://mcp.sportsdata.example/sse"` plus an auth header carrying
the licence. Same catalogue, zero install, but it's infra we run (see §7 trade-offs).

---

## 7. Costs to us

| Item | Cost |
| --- | --- |
| Data-plane hosting | **$0** — self-host (runs on the customer's machine, their IP) |
| Entitlement service | Cloudflare Worker + D1 — **free tier** covers thousands of customers; ~$5/mo only at real scale |
| Transactional email | Resend / Cloudflare Email — free tier |
| Apple notarization (downloadable build) | **$99/yr** Apple Developer ID |
| Stripe fees | 2.9% + 30¢ per charge |

Roughly **$99/yr + Stripe fees, flat regardless of customer count.** The margin on
$15–99/mo subscriptions is almost entirely ours. (Hosted delivery — Option A — would
add server + likely residential-proxy costs for the gambling feeds; that's why
self-host is the launch choice.)

---

## 8. Build phases (each independently shippable)

| Phase | What | Ships |
| --- | --- | --- |
| **0 — Manual MVP** | Payment Links (done) + you email licence + download by hand | this week — validates demand, **zero new infra** |
| **1 — Entitlement service** | Cloudflare Worker + D1; Stripe webhook; `/entitlement`; key issuance | the always-on cheap piece |
| **2 — Licence gate in MCP** | resolve + verify + scope groups; offline cache | enforcement |
| **3 — Downloadable build** | standalone bundle + first-run setup + self-register + notarization | the polished installer |
| **4 — Self-serve add-ons** | in-app Feeds screen → Checkout/Portal → webhook → refresh loop | the "buy more" UX |
| **5 — Fulfilment automation** | webhook → issue key → email download + setup | hands-off |

A good order: **ship Phase 0 to get paying customers now**, build 1→5 underneath, and
flip each on as it's ready.

---

## 9. Open decisions

1. **Entitlement infra** — Cloudflare Worker + D1 (recommended: free, always-on, ideal
   for webhooks), or fold a tiny endpoint onto something already hosted?
2. **First delivery** — the polished **downloadable build** now, or **`uvx` + a config
   snippet** first (faster to ship, dev audience) and add the build in Phase 3?
3. **Distribution if `uvx`** — publish to public PyPI and let the **licence** (not
   secrecy) gate feeds, or keep the package private behind a token'd index?
4. **Licence env var name** — proposed `SPORTSDATA_LICENSE` + `SPORTSDATA_MCP_GROUPS`.
5. **Launch sequence** — flip the live site + Stripe now (Phase 0), or hold until the
   licence gate (Phase 2) is in?
