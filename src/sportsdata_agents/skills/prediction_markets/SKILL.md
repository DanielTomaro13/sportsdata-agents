---
name: prediction_markets
description: How to read prediction markets (Kalshi, Polymarket) — price-as-probability, resolution rules before any comparison, fees/spread, and comparing exchange contracts to book odds honestly.
triggers: [prediction market, kalshi, polymarket, event contract, binary contract, resolution, settlement]
---
# Prediction markets

Prediction markets (Kalshi, Polymarket) trade **binary/event contracts** that pay 1
unit if an outcome happens and 0 if it doesn't. The contract's price therefore *is*
the market's probability: a contract trading at 0.62 means a 62% implied chance.

## Read the resolution rules first
The single biggest mistake is comparing two markets that resolve differently. Before
any comparison, read `market_detail` for the exact question, the settlement source,
and the expiry. "Team X wins the title" and "Team X wins the final" are different
contracts. If the book's market and the prediction market don't resolve on the same
condition, say so and stop — the comparison is invalid.

## Price → probability
- Best bid / best ask (`market_prices`) bracket the fair value; use the mid for a
  point estimate and note the spread (wide spread = thin/uncertain market).
- Do **not** vig-remove a single binary contract price — it's already a probability.
  The exchange takes a fee on settlement, not a two-sided overround like a book.

## The exchange-vs-book edge
This is the high-value play and why this agent exists:
1. Take the contract's implied probability (prediction market).
2. Take the SAME outcome at the sportsbooks (`find_fixture` → `best_prices`), and
   `vig_removal` the book market to a fair probability.
3. Compare. A meaningful gap means one side is mispriced relative to the other —
   report which side is the value, the size of the gap, and the venues. Liquidity and
   the fee/withdrawal frictions on each venue determine whether it's actually takeable.

## Honesty
Surface the probabilities, the gap, and the caveats (thin liquidity, resolution-rule
mismatch, fees). The user decides and acts; this agent never trades or bets.
