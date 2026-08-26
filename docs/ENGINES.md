# Pricing-engine seam

The platform runs fully without a pricing engine. When one is configured,
agents gain **model fair prices** for whole boards — with Monte Carlo error
bars — which powers the `model_value` watch, the consistency-edge scan, and
engine-priced predictions for backtest/CLV.

## Configuration

| Env var | Values | Meaning |
|---|---|---|
| `SPORTSDATA_AGENTS_ENGINE_BACKEND` | `none` (default) / `local` / `remote` | Which backend `quant.engines.resolve_engine()` returns |
| `SPORTSDATA_AGENTS_ENGINE_API_URL` | URL | Hosted pricing API (remote backend) |
| `SPORTSDATA_AGENTS_ENGINE_API_KEY` | secret | Bearer key for the hosted API |

`local` lazily imports an engines package if one is installed in the
environment — this repo never depends on it and degrades cleanly without it.
`remote` is a thin key-authenticated client; until the hosted service is
live it reports unavailable rather than erroring.

## Surfaces

- **`engine_fair_prices` tool** (quant tools): price a fixture's board.
  Quote payloads mirror what any book quotes — racing
  `{win_odds: {runner: odds}}`, footy
  `{h2h: [home, away], total: [line, over, under]}`. With `record: true`
  the prices are stored as predictions under an auto-managed
  `engine:<sport>` model artifact, so the existing value watch, backtest
  and CLV replay them unchanged.
- **`model_value` watch kind**: seeds the engine from a book's own anchors,
  prices the board, and fires where that book's derivative quotes sit
  outside the model's noise band. Params: `sport` (engine sport),
  `price_sport` (warehouse label if different), `book`, `min_edge_pct`
  (default 3), `error_multiple` (default 3 standard errors),
  `max_age_minutes` (default 30 — stale quotes never meet fresh prices),
  `places` (racing). Skips cleanly when no engine is configured.
- **`quant.engine_value.consistency_scan`**: the pure maths — join book
  quotes to engine prices on (market, selection, line), require the edge to
  clear the threshold AND the error band.
- **Advisory tools** (no engine needed): `cash_out_estimate`,
  `slip_redundancy`, `value_board` (edge × confidence × freshness ranking
  with correlated-exposure annotation). Advisory only — the platform never
  places bets.

## The seam's guard rails

`sportsdata-engines` is private, optional, and versions independently of this repo, and
the coupling is **not a published API**: 22 internal symbols across four modules, most of
them bypassing the `price_board_any` seam built to avoid exactly that. Nothing here
imports engines in CI, so an engines refactor that moves one of them surfaces as an
ImportError deep inside a pricing call, in front of a user.

Two things bound that, both in [`quant/engines_contract.py`](../src/sportsdata_agents/quant/engines_contract.py):

- **A version handshake** at the seam's entry point, mirroring `MIN_MCP_VERSION` on the
  MCP side. An engines older than the call sites assume warns at backend selection, not
  mid-price, and the warning says what to do about it. Absence stays silent — running
  without an engine is normal and supported.
- **A declared import inventory** (`EXPECTED_SYMBOLS`), guarded by a test. It does not
  prevent a break; it makes the coupling visible, so adding a 22nd is a deliberate act
  that updates the list. The inventory caught one on its first run that hand-reading had
  missed.

The 21 call sites were deliberately **not** refactored to route through one module. That
code is correct, engines is not installed here or in CI, and a large refactor nothing can
exercise trades a visible risk for an invisible one.

`scripts/capability-audit.py` also prints an **engines/data coverage ledger** — engine
sports with no matched feed (darts, rugby union, snooker) and feeds with no engine (chess,
F1, esports and others). Nothing published that join before, which is why both roadmaps
could advance without either seeing the other.

## Coverage note

The derivative comparison joins on exact (market, selection, line) keys, so
its breadth is the **intersection** of the engine's board ladder and the
book's quoted ladder (an engine board prices ~5 lines per family around its
simulated mean; books quote many more). Full-board family expansion widens
this in a later milestone; the join never fabricates a price for a line the
engine didn't compute.

## Free quant additions (engine-independent)

- **`quant.devig`** — proportional and piecewise-curve de-vig. The curve
  models how books actually shape margin (longshot ramp, flat body,
  compressed favourite tail); on odds-on quotes proportional removal strips
  margin that cannot exist. Fit shape parameters per book from history.
- **`quant.racing_place`** — the textbook Harville (1973) win-to-place
  converter: exact top-1/2/3 probabilities from win odds. Uncalibrated (it
  overrates favourites deeper in the order) but free and far better than
  guessing.
- **`engine_health` tool** — backend status, a timed test price, and 24h
  engine-prediction/alert counts. A silently wrong engine manufactures fake
  edge; check health before trusting a value board.

## Noise discipline

Engine prices carry `std_error`. Every consumer here treats differences
inside the error band as noise: the scan skips them, the board's confidence
term discounts them, and unknown error bars score 0.5 confidence — unknown
certainty is not full certainty. A "value" candidate can still be a model
bias rather than an edge (e.g. structured tail misses show up as apparent
alternate-total value); replay measurement, not a single scan, decides what
is real.
