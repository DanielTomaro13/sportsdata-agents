---
name: compare_odds
description: Cross-bookmaker comparison workflow — find the same selection at every book, surface the best price, and flag value.
triggers: [best price, best odds, compare odds, across books, across bookmakers, line shop, arbitrage, value bet]
---
# Comparing odds across bookmakers

The data plane carries many books behind the same capability tags — use that to line up
one selection across all of them.

## Procedure
1. **Identify the event once.** Resolve the fixture (teams/race, start time) before
   touching prices, so every book's market is matched to the same event.
2. **Collect prices per book.** Use your market/price tools for each available book.
   Record, for every quote: book, selection name as that book spells it, decimal odds,
   and the fetch time. Skip a book rather than guess if its market is missing.
3. **Best price:** call `best_price` over the collected quotes.
4. **Value check (when asked, or when a sharp book is present):**
   - Take the full market at the sharpest book available and call `vig_removal` for a
     fair probability.
   - Call `expected_value` with that fair probability against the best offered odds.
   - Only describe a price as "value" when expected_value > 0; show the number.
5. **Report:** a compact table of book → odds (with fetch times), the best price, the
   fair probability and its source book, and EV if computed. Cite which tools/providers
   every figure came from.

## Rules
- Same selection, same market type, same line — never compare a -10.5 spread at one
  book with -11 at another as if equal.
- Prices move: include fetch times; never present a price as current without one.
- Advisory only: you report and compare — you cannot and do not place anything.
