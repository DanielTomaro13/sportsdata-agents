# Racing board — five books, multi-source discovery, fast inside two hours

> **STATUS: IMPLEMENTED 2026-08-31.** All workstreams shipped. Measured after:
>
> | | before | after |
> |---|---|---|
> | race universe | 413 (TAB only) | **1,314** — 901 of them TAB does not carry |
> | books pricing | 2 | **5** |
> | PointsBet coverage | — | 88% of the active board |
> | Ladbrokes coverage | absent (thought auth-walled) | 88% |
> | Sportsbet coverage | — | 81% |
> | Dabble coverage | absent (thought too slow) | 38% (AU-only catalogue) |
> | races with no book at all | — | **0** |
> | price cache | 60s | none (mcp 0.32.1) |
> | book request rate | 10/s | 200/s, TAB still 2.5/s |
>
> Audit it any time with
> `python -m sportsdata_agents.interfaces.racingboard --coverage`.
>
> Five bugs the implementation found, each of which failed silently rather than
> raising: Ladbrokes' race field is `number` not `race_number` (indexed zero races);
> Sportsbet's `startTime` is an epoch integer, not ISO (every start parsed as None);
> PointsBet's is `advertisedStartDateTimeUtc`; two of the three Ladbrokes category
> UUIDs had been copied from a truncated display and were wrong; and the venue
> normaliser stripped bare month names, so `Del Mar` reduced to an empty token set and
> vanished from the board entirely.
>
> The one design decision worth knowing: `(code, venue, race_no)` is **not** a unique
> race. Townsville runs a day and a night greyhound card, each with a race 1, and
> PointsBet carried 121 such pairs. The advertised start is part of the identity, which
> is also what lets Dabble — which publishes no race number at all — join.

The board today shows one race universe (TAB's), prices from two corporate books, and
polls a 45-minute horizon. This plan takes it to **five books**, a **union discovery
spine**, and **maximum-rate polling from two hours out**.

Everything below is measured, not assumed. Numbers were taken live on 2026-08-31.

---

## What the measurements say

### 1. TAB is the narrowest possible spine

`sources.py:72` builds every `RaceRef` from `tab_racing_meetings`, so the board can never
show a race TAB does not carry. Measured for one day:

**All three codes — thoroughbred (R), greyhound (G) and harness (H):**

| Book | R | G | H | **Total** |
|---|---|---|---|---|
| **PointsBet** | 534 | 435 | 231 | **1200** |
| **Ladbrokes** | 347 | 263 | 100 | **710** |
| **Sportsbet** | 289 | 237 | 95 | **621** |
| **TAB** *(current spine)* | 204 | 135 | 74 | **413** |
| **Dabble** *(meetings)* | 71 | 39 | 27 | **137 meetings** |

PointsBet alone sees ~3× TAB's universe, and TAB is the smallest in **every code**.
Jurisdiction is not the cause — NSW and VIC both return 413.

Dabble is counted in **meetings** because it is meeting-granular (see WS5). Its 137
meetings are **more than any other book** — TAB 65, Ladbrokes 70, PointsBet 124 — and at
19:00 AEST it had **201 open races, every one carrying prices**, across all three codes.
Dabble's active feed drops finished meetings, so that 201 is the still-open card at that
hour rather than a full-day total; the other books' figures are whole-day.

The board already tracks all three (`MF_CODES=R,G,H`), so this is purely a discovery
ceiling, not a scope decision.

### 2. The coverage gaps are name matching, not missing prices

Every book carries the tracks the board reports as uncovered. They spell them differently:

| Board (TAB) | Ladbrokes | Sportsbet |
|---|---|---|
| `MOHAWK` | `Woodbine Mohawk Park` | `Woodbine Mohawk Park` |
| `NORTHFIELD PARK` | `Northfield Park` | `Northfield Pk` |
| `SARATOGA` | `Saratoga` | `Saratoga TB` |

`_venue_compatible` (`sources.py:37`) is exact-or-≥5-char-prefix, so `MOHAWK` never reaches
`Woodbine Mohawk Park`. That single rule is why harness read as 1/11 covered when the books
have essentially all of it.

**`Woodbine` and `Woodbine Mohawk Park` are different tracks** (thoroughbred vs harness).
Any looser matcher must never merge them — the same variant-marker lesson the fixture
resolver learned when a Women's game merged with the men's and manufactured a 74% arb.

### 3. Speed is not the constraint — request volume is

Per-race price call, measured over distinct races (no cache):

| Book | Median | Range | Payload |
|---|---|---|---|
| PointsBet | **0.05s** | 0.04–0.13 | 13 KB |
| TAB | **0.06s** | 0.05–0.07 | 32 KB |
| Ladbrokes | **0.08s** | 0.06–0.21 | 192 KB |
| **Sportsbet** | **0.26s** | 0.13–0.39 | 163 KB |
| Dabble *(per **meeting**)* | **0.31s** | — | small |

Sportsbet — the book already in production — is 4–5× slower than every book being added.

Dabble's row is not like-for-like: 0.31s buys an **entire meeting's** races and prices,
where every other row buys one race. Per race it is the cheapest book on the board. Its
137-meeting sweep took 42.7s wall at 6-way concurrency — that is the spec's
`rate_limit_rps: 3` throttling, **not** latency, and it is exactly the kind of ceiling
WS3's per-book limiter is designed to manage.
Index calls are 0.38–0.63s cold, then ~0.02s from the MCP's 60s GET cache.

Races are **already** fetched concurrently (`poller.py:111`, `asyncio.gather`): 10 races
serial 2.83s → parallel 0.36s, a **7.9× speedup**. Books *within* a race are still serial.

### 4. The two-hour window is 41–86 races

From Ladbrokes' `advertised_start` across a full day:

```
< 10 min      5 races
10–30 min     6
30 min–2h    30
             ──
within 2h    41 races right now
             86 races at the busiest rolling 2h window of the day
```

**86 races × 5 books = 430 corporate requests per cycle** at peak. At ~0.1s median,
parallelised, wall time is under a second — so the binding constraint is bookmaker rate
limits, not latency. That is what the design below is shaped around.

---

## WS1 — Multi-source discovery

Replace the TAB spine with the **union of every book's meetings**.

- **`RaceUniverse`**: each book contributes `(code, venue, race_no, advertised_start)`;
  entries merge into one canonical race list. TAB becomes one contributor, not the
  authority.
- **Canonical identity must stop depending on TAB's `venueMnemonic`** — it does not exist
  for a race TAB does not carry. Move to `(code, venue_canonical, race_no, date)`, keeping
  the mnemonic only as TAB's own handle.
- **Every book implements `build_index` whether or not it also prices** — that is what
  makes it a discovery source.
- Ladbrokes' shape suits this well: `meetings{70}` / `races{710}` / `venues{70}` with
  `advertised_start` per race, which is exactly what the horizon logic needs.

**Accept deliberately:** the universe grows from ~413 to ~1,400 races. WS3 must land with
this, not after it.

## WS2 — Venue resolution

Now the **join key for building the spine**, not merely enrichment — so it gates WS1.

- Normalise harder: strip country tags (`(AUS)`, `(USA)`, `(CAN)`), discipline suffixes
  (`TB`, `TF`), `Extra`, embedded dates; expand abbreviations (`pk`→`park`,
  `jnc`→`junction`).
- Replace exact-or-prefix with **token-subset** matching.
- **Gate on `code` AND `race_no`.** Load-bearing: it is the only thing stopping `Woodbine`
  (R) merging with `Woodbine Mohawk Park` (H).
- A small maintained **alias table**, as data, for cases tokens cannot reach.
- **Ambiguity must refuse.** A wrong merge shows a price from the wrong race, which is
  worse than a gap — the same rule as `unique_team_match`.

## WS3 — Maximum-rate polling inside two hours

The requirement: from 2h out, poll as fast as safely possible. The honest reading of "as
fast as possible" is **as fast as each book tolerates**, discovered rather than guessed.

**Horizon** `45min → 120min`. Everything inside it is "active" — 41–86 races.

**Per-book adaptive rate limiter (token bucket).** Each book gets its own budget and
adapts:

- Start at a conservative sustained rate per book.
- **Ramp up** while responses stay clean.
- **Back off immediately** on 429/5xx, then recover slowly.
- Never let one slow book stall the cycle — books run concurrently per race (WS4).

This is what makes "as fast as possible" safe: the ceiling is measured live per book
instead of hard-coded, and a book that starts rate-limiting degrades rather than getting
the account noticed.

**Priority within the active set.** When the budget cannot refresh everything, nearest-to-
jump wins:

| Band | Priority |
|---|---|
| < 10 min | highest — always refreshed |
| 10–30 min | high |
| 30 min – 2h | normal — the newly-added band |
| > 2h | discovery only |

`max_active_races` becomes **per-band** rather than one global cap of 12, which would
otherwise starve the 30min–2h band entirely.

## WS4 — Per-request cost

- **Parallelise books within a race.** `corporate.enrich` currently walks books serially:
  5 books ≈ 0.47s/race. Fanned out ≈ 0.26s, bounded by Sportsbet. Directly multiplies the
  throughput WS3 can buy.
- **Sportsbet is the tall pole** at 0.26s (5× the next book). Investigate `selectionNames`
  or a lighter racecard variant — the single biggest latency win available.
- **Ladbrokes returns 192 KB/race** — project server-side if bandwidth matters.
- Preserve the 60s GET cache benefit on index calls.

## WS5 — The five books

| Book | Status | Action |
|---|---|---|
| **TAB** | live | Demote from spine to contributor (WS1) |
| **Sportsbet** | live | Keep; trim latency (WS4) |
| **PointsBet** | live | Keep; promote to a discovery source — it has the largest universe |
| **Ladbrokes** | **verified**: 0.08s, 710 races, 0 errors | Add `LadbrokesBook`. The `corporate.py` claim that it "404s without auth" is **stale — delete it** |
| **Dabble** | **verified**: R+G+H, 137 active meetings, prices live, 0.28s/meeting | Add `DabbleBook`. Meeting-granular — cheapest of the five |

### Dabble — verified on all three codes

**Dabble carries R, G and H, with live fixtures and prices.** Measured 2026-08-31:

| Code | `sportName` | Active meetings |
|---|---|---|
| R | `Thoroughbred Racing` | **71** |
| G | `Greyhound Racing` | **39** |
| H | `Harness Racing` | **27** |

Sample rows pulled live — `Ballarat Synthetic` R1 (9 races, Blue Suede Shoes @ 1.45),
`Warrnambool` (13 races, Paw Archie @ 1.60), `Newcastle` Newcastle Herald Pace (8 races,
Rocknroll Tony @ 1.38). Runners and decimal prices, all three codes.

**Discovery: use `dabble_active_competitions` UNFILTERED and key on `sportName`.**

This is the trap, and it cost two wrong conclusions in this document's own drafting:

- **`dabble_sports` is stale and must not be used for racing.** It lists 24 sports with a
  single `Horse Racing` entry, no Greyhound, no Harness — and `isRacing` is `false` on
  every one of the 24, including the racing ones. Filtering discovery by a racing `sportId`
  therefore returns **nothing**.
- The **active-competitions feed is the truth**: its `sportName` values are
  `Thoroughbred Racing` / `Greyhound Racing` / `Harness Racing`, which do not appear in
  `/sports` at all. 269 active competitions, 137 of them racing.
- `dabble_competitions(sportId=Horse Racing)` returns only specials, Pick'em and futures —
  which is what made Dabble look horse-only and empty.

**Shape — and it is the best of the five.** One competition = **one meeting**; one
`dabble_competition_fixtures` call returns **every race in that meeting** with markets,
selections and prices embedded. So Dabble costs **one request per meeting**, not per race,
where the other four books cost one per race. At 0.28s/meeting and ~137 meetings, a full
refresh of every Dabble price on the board is ~137 requests — against ~700+ for a
per-race book. This makes Dabble the *cheapest* book to poll, not the heaviest.

**Two implementation notes.**

- Market names come back **null** in the slim fixtures feed. Identify the win market by
  `resultingType` (`RacingFixed*` / `RacingSP*`), which the spec's `classify` block already
  tags as `product: racing` — never by name. `RacingSrm*` (Same-Race-Multi) and
  `RacingDD*` (exotics) must be excluded from a win-price comparison.
- Meetings that have finished for the day return **0 fixtures**. That — not a missing
  catalogue — is what an empty probe means.

**The `corporate.py` claim that Dabble is "too heavy for fast polling" is wrong twice
over** — it is meeting-granular and among the fastest books measured. Delete it with the
Ladbrokes one.

**Spec follow-up (sportsdata-mcp):** `dabble_sports`' summary claims it lists the sports
including `Horse Racing`, and the racing guidance points at the sport tree. Both are
misleading for racing. The spec should document `dabble_active_competitions` +
`sportName` as the racing discovery path, and note that `isRacing` is unreliable.

## WS5b — Per-book code mapping (R/G/H)

The code gate in WS2 is only as good as each book's code assignment, and every book spells
it differently. A wrong mapping silently drops a whole discipline — which is exactly how
harness came to look empty.

| Book | Field | Mapping |
|---|---|---|
| TAB | `raceType` | already `R`/`G`/`H` |
| PointsBet | `racingType` | `1→R`, `2→H`, `3→G`, `4→G` (existing `PB_TYPE_TO_CODE`) |
| Sportsbet | `className` | **six values**: `Horses - Aus/NZ`, `Horses - International`, `Horses - Asia` → R; `Greyhound Racing` → G; `Harness Racing`, `Harness Racing - International` → H |
| Ladbrokes | `meeting.category_id` | `4a2788f8…`→R (347), `9daef0d7…`→G (263), `161d9be2…`→H (100) — decoded from meeting names, **verify before shipping**: ids may not be stable |
| Dabble | `sportName` on **active competitions** | `Thoroughbred Racing`→R, `Greyhound Racing`→G, `Harness Racing`→H. **Never `/sports`** — it lists none of them and `isRacing` is false on all 24 |

Ladbrokes' category ids are opaque UUIDs decoded by inspecting meeting names (Corowa →
thoroughbred, Solvalla → harness, Shepparton → greyhound). Resolve them by name at
startup rather than hard-coding, or a silent re-issue of an id mislabels a whole code.

## WS6 — Frontend

- `app.js:13` — add `ladbrokes: "LB"`, `dabble: "DAB"` to `BOOK`, plus brand colours.
- The BEST column and per-book tooltip already iterate the `corp` dict generically, so
  **no render changes are needed** — they will simply show more books.

## WS7 — Measure it, then guard it

"The board looks thin" has no symptom, exactly like the market-dictionary drift problem.

- **Per-book coverage metric** each cycle: of the universe, what fraction did each book
  match?
- **`racingboard coverage`** audit command printing the per-book table.
- **Regression guard** so a normalisation change that silently drops matches fails loudly.
- **Guard the other direction too** — a sudden coverage *jump* can mean false merges.
- **Rate-limit telemetry**: per-book request rate, 429 count, achieved refresh interval.
  Without it, "as fast as possible" is unmeasurable.

---

## Sequence

1. **WS2 venue resolver + WS7 coverage metric** — the multiplier, and the means to prove it
2. **WS1 multi-source discovery** — depends on WS2 as its join key
3. **WS3 tiering + rate limiter** — must land with WS1 or volume triples unguarded
4. **WS5 Ladbrokes and Dabble** — both verified; Dabble also adds meeting-granular
   discovery for all three codes
5. **WS4 speed** → **WS6 frontend** → **WS7 guards**

## Risks

- **Universe growth 413 → ~1,400** is the main risk. WS3 must land with WS1.
- **Rate limits, not latency, are the ceiling.** The adaptive limiter is the mitigation;
  telemetry is how we know it is working.
- **Over-matching is worse than under-matching** — a wrong price on the wrong race beats a
  visible gap. Refuse on ambiguity.
- **Dabble's `/sports` catalogue lies about racing.** Discovery must key on `sportName`
  from the active-competitions feed; a future refactor that "tidies" it back onto `sportId`
  silently drops all 137 racing meetings. Worth a guard.
- **Sportsbet dominates per-race cost** at 0.26s and will bound any within-race fan-out.

---

## Review before execution

The plan was reviewed against the code on 2026-08-31. Seven findings; **two are blockers
that change what gets built first.**

### B1 — RESOLVED 2026-08-31 (was a blocker)

`config.py:19` sets `CACHE_TTL_DEFAULT = 60.0`, applied to **every GET response** per
provider. **No racing endpoint anywhere carries `never_cache`** — not `tab_racing_race`,
`sportsbet_racecard`, `pointsbet_racing_meeting`, `entain_racing_racecard` nor
`dabble_competition_fixtures`. The agents side never sets `SPORTSDATA_MCP_CACHE_TTL`.

Proven live on a meeting with 360 prices — five calls over 12 seconds:

```
call 1:  13ms  races=12 prices=360 sha=da53fa56d0e6
call 2:  24ms  races=12 prices=360 sha=da53fa56d0e6
call 3:  25ms  races=12 prices=360 sha=da53fa56d0e6
call 4:  25ms  races=12 prices=360 sha=da53fa56d0e6
call 5:  12ms  races=12 prices=360 sha=da53fa56d0e6
```

Byte-identical, 12–25ms against 311ms cold. Every repeat was the cache.

**This invalidates the headline requirement.** "Maximum-rate polling from two hours out"
cannot beat a 60s cache: poll every 2s and 29 of every 30 calls return the same bytes.
It also means **the board's current `price_interval=8` is already a fiction** — it polls
8-secondly for data that refreshes at most once a minute. Every measurement of "how fast
can we poll" in this document is a measurement of the wrong thing.

**Fixed** in sportsdata-mcp `ab0c465`: `never_cache: true` on the six racecard endpoints
— `tab_racing_race`, `pointsbet_racing_race`, `sportsbet_racecard`,
`entain_racing_racecard`, `dabble_competition_fixtures`, `dabble_fixture_details`.
Verified after the change: discovery stays a flat 3-4ms cache hit while prices go to the
network every call (14-60ms, varying). `test_price_freshness` pins both halves of that
split, and mutation-testing confirms the guard fails when the flag is removed.

Racing **discovery** endpoints keep the cache deliberately — per-day data, ~100x on repeat.

**The volume this releases, and why it is safe today.** The board's cadences are now real
rather than notional. TAB was effectively 1 request per race per 60s; at
`price_interval=8` it is now 7.5x that. At today's `max_active_races=12` that is
**~1.5 rps against TAB's spec limit of 2.5** — inside budget, and the per-provider token
buckets in the MCP are untouched by this change, so they remain the throttle. Removing
the cache did not remove rate limiting.

**But it makes finding 4 the binding constraint for WS3.** At WS3's target of 41-86 active
races, `86 / 8s` is **~10.75 rps against TAB's 2.5** — over budget by 4x. So WS3 cannot
simply widen the horizon: it must raise the spec limits deliberately per book, lengthen
intervals, or both. That trade is now the first thing WS3 has to size, and it is
measurable rather than guessed.

### B2 (BLOCKER) — Dabble has no race number, and the canonical key requires one

Fixture keys are `id, name, advertisedStart, actualStart, competition, competitionId,
competitionName, country, status, state, isDisplayed, created, updated, markets,
selections, prices`. There is **no `raceNumber`**, and `name` is the race's *name*
(`"Sportsbet Final"`, `"Brandt 3YO Maiden Plate"`) — not its number.

WS1/WS2 key races on `(code, venue_canonical, race_no, date)`. Dabble cannot supply
`race_no`, so as written **Dabble joins nothing and contributes zero prices** despite
being verified on all three codes.

**Fix:** the canonical race must carry **both** `race_no` and `advertised_start`, and the
resolver must accept a match on either. Dabble joins on
`(code, venue_canonical, advertised_start)` — start times are exact to the minute and
unique within a meeting, so this is as strong a key as the number. Every other book
supplies both.

### 3 — Runner-level matching is absent from the plan, and is where prices actually join

The whole plan operates at venue/race level, but a price is per **runner**.
`corporate.py` joins them on `_norm_runner` — name only. And unlike `_norm_venue`, which
cuts at `(`, **`_norm_runner` does not**: it strips a leading saddlecloth number and
non-alpha characters, so `Jadzia (NZ)` normalises to `jadzianz` and never meets `Jadzia`.

Not yet biting on the AU sample checked (`Blue Suede Shoes`, `Don't Doubt Tigga` — all
clean). But country suffixes are a convention on **imported and international** runners,
which is precisely the coverage this plan is adding: Saratoga, Woodbine, Northfield Park.
Expect it to bite exactly when WS1 lands.

**Add a workstream.** Same rules as WS2 — cut at `(`, refuse on ambiguity — plus the
existing runner-number↔name bridge as a cross-check where a book gives both.

### 4 — The real rate ceiling is in the MCP specs, not the agents-side limiter

WS3 designs an adaptive per-book limiter in the agents layer, but each spec carries its
own fixed `rate_limit_rps`: **TAB 2.5, Dabble 3, and Sportsbet / PointsBet / Entain none
at all.** The agents-side limiter cannot exceed a ceiling set below it — Dabble's
137-meeting sweep took 42.7s wall purely because of `rps: 3`, not latency and not the
book pushing back.

So WS3 spans both repos: the adaptive limiter is useless unless the spec-level limit is
raised to meet it, and the three books with *no* limit are the ones actually exposed.

### 5 — Dabble breaks the per-race polling model, and the request estimate with it

WS3 tiers by race ("nearest to jump"). Dabble fetches by **meeting** — one call returns
races spread across hours, landing in several bands at once. It cannot be scheduled per
race like the others.

This also makes `86 races × 5 books = 430 requests` wrong: Dabble's share is per meeting,
so the real figure is nearer `86 × 4 + ~20 = ~364`, and a full Dabble refresh is ~137
requests against ~700 for a per-race book. **Schedule Dabble per meeting, at the band of
its earliest unfinished race.**

### 6 — The sequence contradicts itself

The plan states "WS3 must land with WS1, not after it", then sequences WS1 as step 2 and
WS3 as step 3. With B1 folded in, these are one deployable unit: **discovery, tiering,
limiter and cache control ship together, or the board triples its volume unthrottled.**

### 7 — "~1,400 races" is a sum presented as a union

Book totals sum to 2,944 (1200+710+621+413). The plan asserts ~1,400 after dedupe without
showing the working, and WS3's whole sizing rests on it. Measure the actual union early —
it is a by-product of the WS2 resolver and should be the first number WS7 reports.

### What survives review unchanged

The two load-bearing diagnoses hold. **Coverage is a name-matching problem, not a missing-
price problem** — every book carries the tracks reported uncovered. And **volume, not
latency, is the constraint**. The refusal-on-ambiguity rule, and the per-code gate that
keeps `Woodbine` apart from `Woodbine Mohawk Park`, are the right shape.

### Revised sequence

1. **WS2 venue resolver + runner normalisation (finding 3) + WS7 coverage metric** — the
   multiplier, and the means to prove it. Report the true union size (finding 7).
2. **Canonical key carrying both `race_no` and `advertised_start`** (B2) — gates Dabble.
3. **WS1 + WS3 + B1 as ONE change** — discovery, tiering, adaptive limiter, spec rate
   limits (finding 4), Dabble scheduled per meeting (finding 5), cache control last within
   the change.
4. **WS5 Ladbrokes + Dabble** → **WS4 speed** → **WS6 frontend** → **WS7 guards**
