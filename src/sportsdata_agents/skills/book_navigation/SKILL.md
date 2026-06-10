---
name: book_navigation
description: Verified bookmaker entry points (competition/event ids) for cross-book price lookups, starting with AFL.
triggers: [afl, australian rules, aussie rules]
---
# Bookmaker navigation — verified entry points

Bookmaker APIs are id-mazes; guessing ids burns tool calls. Use these VERIFIED routes
(live-checked 2026-06-10) instead of exploring.

## AFL (Australian Rules)

**Sportsbet** — AFL competitionId = **4165** (class "Australian Rules"):
- `sportsbet_competition_matches` with `competitionId: 4165` → this round's events
  (id, "Western Bulldogs v Adelaide Crows", startTime). Event LISTS are small;
  the per-event Markets feed is ~2 MB and will be size-blocked — expect that.

**PointsBet** — AFL competitionKey = **7523** (NOT 37):
- `pointsbet_competition_events` with `competitionKey: 7523`. Warning: ~1 MB feed,
  may be size-blocked.

**TAB** — names, not ids: sport **"AFL Football"**, competition **"AFL"**:
- `tab_sport_next_to_go` / match tools with those names; match names like
  "Adelaide v Geelong" (pass raw names — encoding is handled).

## When a price feed is size-blocked
Bookmaker price firehoses often exceed the response cap ("Response was N bytes…").
Do NOT retry the same call. State which book was unavailable and continue with the
books that answered — two priced books is a valid comparison. Cross-book snapshot
queries become first-class when the ingestion store lands (P2).

## Rules
- Two books priced on the same match is enough for best-price; don't exhaust the
  budget chasing more.
- Then: `best_price` per side, `vig_removal` on the fuller market, `expected_value`
  against the best price. Cite book + fetched_at per figure.
