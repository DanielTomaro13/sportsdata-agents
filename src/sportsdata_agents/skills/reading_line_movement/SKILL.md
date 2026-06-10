---
name: reading_line_movement
description: Interpreting captured line movement — steam vs drift, why the close is the benchmark, and how movement context changes a value report.
triggers: [line movement, steam, drift, market move, line moved, price history]
---
# Reading line movement

`query_line_movement` gives the change-point series. What it means:

- **Steam (price shortening)**: money agrees with the side. If your model's edge
  was computed at the OLD price, it may already be consumed — recompute at the
  current price before calling it value. Fast multi-book steam usually means
  sharp/insider flow.
- **Drift (price lengthening)**: the market disagrees with your model. Drift into
  your pick raises the price (more apparent edge) while telling you informed money
  leans the other way — report BOTH facts, never just the bigger edge number.
- **The close is the benchmark**: the most informed market state. Persistent edge
  vs the close (CLV) is the strongest evidence a process works (quant_concepts).
  When an event is settled, compare entry vs close in the report.
- **No movement** = one first-sighting row, not "no data" — the price simply hasn't
  changed since capture began. Say which.
- **Cross-book divergence**: one book lagging a market-wide move is the classic
  value window — flag the book and the lag explicitly when the series shows it.

Always state the capture cadence honestly: a 5-minute feed cannot see intra-minute
steam, and gaps in the series are coverage gaps, not calm markets.
