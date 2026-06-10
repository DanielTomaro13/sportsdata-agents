---
name: vig_removal
description: Remove the bookmaker margin (vig/overround) to estimate fair probabilities and spot value.
triggers: [vig, overround, juice, fair price, fair probability, fair odds, true price]
---
# Removing the vig

A bookmaker's quoted prices imply probabilities that sum to MORE than 1 — the excess is
the margin (vig / overround / juice). To estimate what the market really thinks, remove it.

## Procedure
1. Collect the decimal odds for **every selection in the same market** at one bookmaker
   (all runners / both sides). Partial markets give wrong answers.
2. Call the `vig_removal` tool with those prices. It returns:
   - `overround` — e.g. 1.052 means a 5.2% margin
   - `fair_probabilities` — each selection's implied probability normalised to sum to 1
3. To compare value across books: get the fair probability from the **sharpest market
   available** (Pinnacle / Betfair exchange when present — lowest overround), then call
   `expected_value` with that probability against each book's offered odds.

## Rules
- Never average odds across books before removing vig — remove per book, then compare.
- A two-way 1.90/1.90 market is ~5.26% vig and 0.50/0.50 fair — a useful sanity check.
- If you cannot get the FULL market at one book, say so; do not normalise a partial set.
- Report the overround alongside any "fair" number so the user sees the margin you removed.
