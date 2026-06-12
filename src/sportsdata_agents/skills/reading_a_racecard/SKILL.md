# Reading a racecard

A racecard is the runners for one race with the facts that move prices. Read it in
this order, and never quote a price without checking for late scratchings first.

## Fields that matter
- **Barrier (gate/box)** — inside draws suit some tracks/distances; wide draws cost
  ground. Note it; don't over-weight it.
- **Weight** — handicaps carry weight to level the field; a big weight rise on a
  proven horse is a negative, a drop a positive.
- **Jockey / driver / trainer** — strike-rate context, not destiny. Top stables and
  hoops shorten prices; that's often already in the market.
- **Form string** — recent placings (e.g. `1-3x2`): `x` = a spell, numbers = finish
  positions, `0` = out of the placings. Recency and class of those runs matter more
  than the raw numbers.
- **Scratchings** — a scratched runner changes every other runner's chance and the
  market percentage. ALWAYS re-pull the card for scratchings before quoting.

## Turning prices into probability
- Win market: convert each price to an implied probability (`1/price`), sum them —
  the total exceeds 1 by the **overround** (the book's margin). `vig_removal`
  normalises the field to fair probabilities; compare fair prob to the price you can
  actually get.
- Place markets and each-way pay fractions of the win odds — never compare a win
  price to a place price as if they're the same market.

## Cross-book and exotics
- The best win price is rarely at the same book across runners — quote the book and
  the price, per runner (`best_price`).
- For multis/SRM, surface the book's `same_race_multi` suggestions and their combined
  price; don't hand-roll combinations or imply a guaranteed collect.

## Honesty
Racing is high-variance. Report form, fair probability and the best price; the user
decides. Never call a horse a "good thing", a "lock", or a "special".
