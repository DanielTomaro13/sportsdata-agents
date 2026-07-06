# Same-Game-Multi (SGM) capture kit

SGM / "same game multi" pricing lives behind session auth on the Dabble and
Sportsbet apps — it is NOT on any anonymous endpoint the discovery walkers can
reach. To wire an SGM feed the operator captures the app's own traffic once, we
read the request/response shapes out of the capture, and a fetcher + normalizer
are written to match (exactly how every other book's feed was built).

This is the ONE data source that needs ~20 minutes of the operator. Everything
here is read-only traffic inspection of your own device — no credentials are
ever shared with the codebase; only the endpoint SHAPES are.

## What we need from the capture

For a single game with an SGM/"build-a-bet" tab open:

1. The **request** that prices a multi: URL, method, headers (which auth header
   carries the session — we need the NAME, never the value), and the JSON body
   (the leg selection ids + market ids being combined).
2. The **response**: the priced multi (the combined odds) and, ideally, the
   per-leg fair/true prices if the app exposes them.
3. The **discovery** call that lists a game's SGM-eligible markets/legs (so the
   fetcher can enumerate legs the way `fetch_dabble_all` enumerates fixtures).

## How to capture (pick one)

### Option A — mitmproxy on the Mac (recommended, 15 min)

```bash
brew install mitmproxy
mitmweb        # opens the web UI at http://127.0.0.1:8081, proxy on :8080
```

- On the **phone**: Settings → Wi-Fi → (your network) → Configure Proxy →
  Manual → server = the Mac's LAN IP, port = 8080.
- Install the mitm cert: browse to `http://mitm.it` on the phone, install +
  **trust** the iOS/Android profile (Settings → General → About → Certificate
  Trust Settings on iOS).
- Open the Dabble (then Sportsbet) app, open a game, open the **SGM / Same Game
  Multi** tab, add 2-3 legs, let it price. Do this for one AFL/NRL game and one
  racing race if racing SGM exists.
- In mitmweb, filter the flow list to the app's API host, find the pricing
  call, and **File → Export** the relevant flows (or save the whole session).

> Certificate pinning: if the app refuses to connect through the proxy, it pins
> its cert. Dabble historically does NOT pin its price API; Sportsbet's web
> surface (`sportsbet.com.au`) is reachable and already captured — the SGM
> price route there may be the same host. If a native app pins, use Option B.

### Option B — the web app in a desktop browser (no proxy, 10 min)

Sportsbet and Dabble both have web apps. Open one in Chrome, DevTools →
**Network** tab, filter **Fetch/XHR**, open a game's SGM builder, add legs, and
watch for the pricing request. Right-click it → **Copy → Copy as cURL** and
paste that here — the cURL has everything (URL, headers, body).

## What to hand back

Paste (or save to `scratchpad/`) any of:
- the exported mitm flows, or
- the "Copy as cURL" of the SGM pricing request + one response body, or
- even a screenshot of the DevTools request/response tabs.

Redact the session token VALUE if you like — we only need to know which header
name carries it, because it goes in an env var (`DABBLE_SESSION` /
`SPORTSBET_SESSION`), never in code, exactly like the DataGolf/TAB keys.

## What we build from it

Once the shapes are known:
1. A spec endpoint per book in `sportsdata-mcp` (auth: a `static_header` reading
   the session env, marked `optional` so keyless installs skip it).
2. `fetch_dabble_sgm` / `fetch_sportsbet_sgm` in `operations/ingestion/fetchers.py`
   (discover eligible legs → price the combinations we care about).
3. `normalize_*_sgm` emitting `PricePoint`s under a `sgm:*` market family.
4. An `sgm_value` watch: the engine already prices SGMs (`engine_sgm_quote`,
   with the confirmed h2h-draw and correlation rules) — the watch compares the
   book's combined price to the engine's fair SGM price and alerts the edge.
   SGM is the softest market class because books price legs as if independent;
   the engine's correlation model is the entire edge.

The engine side is DONE (sgm.py, staking, the book-profile rules). This kit is
only about getting the capture surface.
