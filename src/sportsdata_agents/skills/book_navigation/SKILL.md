---
name: book_navigation
description: Verified bookmaker entry points (competition/event ids) for cross-book price lookups, starting with AFL.
triggers: [afl, australian rules, aussie rules]
---
# Bookmaker navigation — verified entry points

Bookmaker APIs are id-mazes; guessing ids burns tool calls. Use these VERIFIED routes
(live-checked 2026-06-10) instead of exploring.

## Resolving ids (any sport)

1. `lookup_book_ids` with the sport/competition name (e.g. "AFL", "NBA", "rugby")
   → verified ids per book from the weekly catalogue. Never guess ids.
2. Then the book's event-list route with that id, e.g.:
   - Sportsbet: `sportsbet_competition_matches(competitionId=<id>)` → events; per-event
     Markets is a ~2 MB firehose and will be size-blocked — expect that.
   - PointsBet: `pointsbet_competition_events(competitionKey=<id>)` — ~1 MB feed, may
     be size-blocked.
   - TAB: name-based paths — sport/competition NAMES from the lookup (e.g.
     "AFL Football" / "AFL"), match names like "Adelaide v Geelong" (pass raw names).

<!-- AUTO:BEGIN refresh-books -->
*Catalogue auto-verified 2026-06-10 by `agents refresh-books`:*

- **Sportsbet** (`sportsbet_nav_hierarchy`): 354 named ids harvested
- **PointsBet** (`pointsbet_sports_list`): 228 named ids harvested
- **TAB** (`tab_sports`): 290 named ids harvested

Resolve ANY sport/competition/market id with the `lookup_book_ids` tool (e.g. query "NBA", "AFL", "rugby") instead of guessing.
<!-- AUTO:END refresh-books -->

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
