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

> **What Base actually is — the product boundary.** Base = the **`sportsdata-mcp` data
> plane only**: the data *tools*, wired into the customer's own AI client (Claude
> Desktop, Cursor, ChatGPT…) with their own model key. It includes **no agents, no chat
> UI, nothing from `sportsdata-agents`**. The "downloadable build" for Base is a small
> **MCP-only setup utility** — *not* the agent workbench. The full app (agent team, chat,
> conductor, alerts) is the **Plus/Pro** product, coming soon, and a **separate download**.
> The site states this plainly and lists exactly what each feed provides; the same
> capability list should appear in the fulfilment email / account page.

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

### Policy: geo-restriction & no refunds on geo-restricted feeds

Some gambling/odds feeds are **geo-restricted** and may not work from a given customer's
location (the bookmaker blocks their region). Policy:

- **No refunds** are given for a gambling feed that turns out to be geo-restricted where
  the customer is. They are responsible for checking availability before subscribing.
- This must be **surfaced in three places**: (1) the **site** (next to the gambling
  add-on — done), (2) **at the point of purchase** — the app/checkout requires an explicit
  **"I understand this feed may be geo-restricted and is non-refundable"** acknowledgement
  before adding a gambling feed, and (3) the **welcome / receipt email**.
- Sport data feeds are not affected — this notice is gambling-feeds only.
- Where we can already detect a likely block (the provider-status **"blocked"** signal
  from the workbench), **warn inline before purchase** so they don't buy a feed we know
  won't reach them.

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
2. The entitlement service (it holds the Stripe secret key) **increments the *Extra
   sport* line-item quantity on their existing subscription via the Stripe API** —
   Stripe prorates and charges the **card already on file**. (A static Payment Link
   can't modify an existing subscription, so this is a server-side API call, not a new
   Checkout. For customers without a saved card, fall back to the Stripe Customer
   Portal, which supports quantity changes.)
3. Stripe fires the `customer.subscription.updated` webhook.
4. Service bumps `sport_slots: 6` → re-signs the entitlement.
5. App **refreshes** (button, or on next launch) → shows the new free slot → they
   **assign** their 6th feed (it's added to the signed entitlement).
6. They **restart their AI app** → the MCP re-reads the licence, sees the new grant, and
   the new tool appears.

> **What actually changes on their machine: nothing but a restart.**
> - **No re-download** — the binary already contains *every* provider; the licence just
>   unlocks one more.
> - **No config edit** — the feed list lives in the licence, not the config file.
> - If their client supports live tool-list updates, the feed can even appear **without**
>   a restart (best-effort via `tools/list_changed`); a restart is just the reliable
>   instruction we give.

### Adding a gambling feed
Identical, using the *Gambling* line item (**+$15/mo**) and the catalogue filtered to
odds providers — **plus a required acknowledgement** before checkout: *"This feed may be
geo-restricted in my region and is non-refundable if it is."* (See the geo-restriction
policy in §3.) If we already detect the feed as **blocked** for them, warn before they
can buy it.

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
exact block for them** (pre-filled with their key), but here's what it is and how it
works for each client. Every MCP client speaks the same pattern: run a command (the MCP
server) and pass the customer's licence (`SPORTSDATA_LICENSE`).

**The licence _is_ the feed list.** The MCP serves exactly the feeds that licence grants
(fetched + verified on startup), so the config carries **only the key** — no group list.
That's the whole reason **adding a feed later never touches their config** (see §4).
Power users can optionally add `SPORTSDATA_MCP_GROUPS` to load just a *subset* of what
they own in a given client.

> `command` is either `uvx` (if they installed via uvx) **or** the absolute path to
> the bundled binary inside the downloadable app — e.g.
> `/Applications/sportsdata-mcp.app/Contents/MacOS/sportsdata-mcp`. With the
> downloadable build the customer needs **no Python at all**.

### The customer never hand-writes any of this — we auto-generate it

The moment they **select their feeds** (in the app's Feeds screen, or a web account
page), we generate three things automatically:

1. **The exact config block** for their chosen client, pre-filled with their licence key
   — e.g. for Claude Desktop:
   ```json
   { "mcpServers": { "sportsdata": {
       "command": "uvx",
       "args": ["sportsdata-mcp"],
       "env": { "SPORTSDATA_LICENSE": "sd_live_xxxx" } } } }
   ```
   (the feeds come from the licence, so the block is identical no matter how many feeds
   they own)
2. **Tailored, numbered instructions** for that client — exact config-file path, where to
   paste, and "fully quit and reopen" the app.
3. A **Copy** button — and in the downloadable app, a **"Set it up for me"** button that
   writes the config file directly, so there's no paste at all.

**The generator's inputs:** selected groups · target client (Claude Desktop / Cursor /
VS Code / other) · licence key · `command` (`uvx` vs the bundled-binary path).
**Outputs:** the filled JSON block + the per-client steps.

**One generator, used everywhere:**
- the **in-app Feeds screen** (shows the block + a "Set it up for me" button),
- the **fulfilment email** (the welcome email already contains their ready-to-paste
  block + steps — so even manual Phase-0 onboarding is one paste for them),
- a re-printable **"Setup" view** on the account page.

The generator runs at **setup** (and if they switch clients). Because the licence
drives the feeds, **buying a feed does NOT regenerate the config** — see §4: only the
entitlement changes server-side, and the customer just restarts their AI app.

The exact per-client blocks it produces:

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
      "env": { "SPORTSDATA_LICENSE": "sd_live_xxxxxxxx" }
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
      "env": { "SPORTSDATA_LICENSE": "sd_live_xxxxxxxx" }
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
      "env": { "SPORTSDATA_LICENSE": "sd_live_xxxxxxxx" }
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
| **3 — Downloadable build** | standalone bundle + first-run setup + **config generator** ("Set it up for me" writes the client config) + notarization | the polished installer |
| **4 — Self-serve add-ons** | in-app Feeds screen → **geo/refund acknowledgement on gambling feeds** → Checkout/Portal → webhook → entitlement updates → app refresh + restart (no config change) | the "buy more" UX |
| **5 — Fulfilment automation** | webhook → issue key → email with the **generated config block + steps** + download link | hands-off |

> The **config generator** (selected feeds + client + key → ready-to-paste block +
> per-client instructions, with copy / "set it up for me") is a small shared utility
> used by Phases 3, 4 and 5 — and even Phase 0's manual emails. Worth building early so
> every onboarding path is one paste (or one click).

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

---

## 10. Pre-launch checklist & design principles

### Policies (drafted — `site/terms.html`, `privacy.html`, `refunds.html`)
Linked from the site footer. **Starter templates — have a lawyer review before relying on
them**, and fill the placeholders (legal entity, contact, ABN, governing law). The
**Refund Policy** keeps the geo-restriction exclusion as a *specific* carve-out and does
**not** claim a blanket "no refunds" (under Australian Consumer Law statutory guarantees
can't be waived) — wording still wants a professional eye.

### Design principles baked into the build
- **Enforcement is best-effort, by design.** Self-host means the binary runs on the
  customer's machine; a determined user can patch out the licence check. The signed
  entitlement stops casual sharing — the real moat is **maintained, working feeds +
  updates**, not DRM. Don't over-invest in copy protection.
- **Licence key travels in a header, never the query string.** The entitlement endpoint
  takes the key via an `Authorization` header (or POST body) so keys never land in
  access logs. (Built in Phase 1.)
- **BYO upstream key.** Only **X/Twitter** needs the *customer's own* provider key; the
  catalogue marks it 🔑 and the feed picker prompts for it (Phase 4). **DataGolf** and
  **La Liga** keys are **provided by us** (La Liga's is a public key; DataGolf uses our
  key). ⚠ **Self-host caveat:** shipping *our* DataGolf (paid) key inside customer builds
  would expose it — for self-host, either proxy DataGolf through the entitlement service,
  or treat DataGolf as "use our hosted access / BYO key", not an embedded secret. Decide
  in Phase 2/3.
- **Tight revocation.** The ≈7-day offline grace is for honest outages only — revoke
  immediately on `customer.subscription.deleted`, and use a **short grace (≤24h) on
  chargebacks / payment failure**. (Built in Phases 1–2.)
- **Update channel.** When a site changes and a feed breaks, downloadable-build customers
  get fixed via an auto-update check — the OTA `datafeed` seam already ships spec
  updates; binary updates add a version ping + "update available" prompt. (Phase 3.)

### Verdict
**Engineering: ready to go.** Phase 0 (manual) + Phases 1–2 proceed now in Stripe **test
mode**; the design principles above are folded into those phases. Policies are drafted
and linked; recommend a legal pass on the wording before live charges, but nothing blocks
building or a soft launch.
