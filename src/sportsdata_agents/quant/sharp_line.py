"""The sharp line for a sports game, and the value of the books against it.

The sports board's spine. Prediction markets and the exchange are the sharpest,
lowest-margin prices going; the bookmakers carry margin and lag. So the FAIR is
built from the sharps, and the books are measured against it.

- **Sharp sources** (in priority-agnostic blend): Kalshi, Polymarket, Betfair,
  Pinnacle. Each is de-vigged on its own (proportional: fair_prob = (1/odds) /
  Σ(1/odds) — the same method quant/devig uses), then the de-vigged probabilities
  are AVERAGED across whichever sources priced the game, and renormalised. A US
  game gets all four; an AFL game gets Betfair + Pinnacle. Averaging de-vigged
  probabilities (not odds) keeps a fat-margin source from dominating.

- **Book value** per selection = best available book price * sharp_prob - 1.
  Positive = the book is longer than the sharps say it should be. Guarded
  against the longshot/data-artifact regime.

Everything here is pure — it takes quotes and returns numbers, so it tests
without a warehouse and can back a board, an alert, or a report.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "SHARP_SOURCES",
    "blend_sharp",
    "book_value",
    "devig",
    "sharp_line",
]

# the low-margin references, order-agnostic (the blend averages, it doesn't rank)
SHARP_SOURCES = ("Kalshi", "Polymarket", "Betfair", "Pinnacle")
_MAX_FAIR_PRICE = 15.0  # value only where the fair is trustworthy (not deep dogs)
_VALUE_BAND = 60.0      # a bigger "edge" is a stale/mismatched quote, not value


def devig(quotes: dict[str, float]) -> dict[str, float]:
    """Proportional de-vig of one source's complete market into fair probs.
    Returns {} when a price is missing or non-positive (an incomplete book
    can't be de-vigged honestly), or when fewer than two selections are
    priced — normalising a lone side trivially yields probability 1.0, which
    surfaced on the board as a nonsense "$1.00 / 100% sharp" line and fed
    SGM legs a certainty."""
    if len(quotes) < 2:
        return {}
    inv: dict[str, float] = {}
    for sel, odds in quotes.items():
        if not odds or odds <= 1.0:
            return {}
        inv[sel] = 1.0 / odds
    total = sum(inv.values())
    if total <= 0:
        return {}
    return {sel: v / total for sel, v in inv.items()}


def blend_sharp(
    by_source: dict[str, dict[str, float]],
    *, sources: tuple[str, ...] = SHARP_SOURCES,
) -> dict[str, Any]:
    """Blend the sharp sources present into one fair distribution.

    ``by_source`` = {source_name: {selection: decimal_odds}}. Only the sources
    in ``sources`` with a COMPLETE, de-viggable market contribute. Returns
    {"fair": {selection: prob}, "sources": [names used], "n": count} — empty
    fair when no sharp priced the game."""
    devigged: list[dict[str, float]] = []
    used: list[str] = []
    for name in sources:
        q = by_source.get(name)
        if not q:
            continue
        d = devig(q)
        if d:
            devigged.append(d)
            used.append(name)
    if not devigged:
        return {"fair": {}, "sources": [], "n": 0}
    # average the de-vigged probabilities across sources, then renormalise
    selections: set[str] = set()
    for d in devigged:
        selections |= set(d)
    summed = {sel: sum(d.get(sel, 0.0) for d in devigged) for sel in selections}
    total = sum(summed.values())
    fair = {sel: v / total for sel, v in summed.items()} if total > 0 else {}
    return {"fair": fair, "sources": used, "n": len(used)}


def book_value(
    by_book: dict[str, dict[str, float]], fair: dict[str, float],
) -> dict[str, dict[str, Any]]:
    """Best book price + value% per selection, measured against the sharp fair.

    ``by_book`` = {book: {selection: odds}}. Returns {selection: {best_odds,
    best_book, value_pct, fair_odds}}. value_pct is None where the fair is a
    deep longshot (untrustworthy) or the edge is implausibly large."""
    out: dict[str, dict[str, Any]] = {}
    for sel, prob in fair.items():
        if prob <= 0:
            continue
        best_odds = None
        best_book = None
        for book, quotes in by_book.items():
            odds = quotes.get(sel)
            if odds and odds > 1.0 and (best_odds is None or odds > best_odds):
                best_odds, best_book = odds, book
        fair_odds = round(1.0 / prob, 2)
        value_pct = None
        if best_odds and fair_odds <= _MAX_FAIR_PRICE:
            edge = (best_odds * prob - 1.0) * 100.0
            if -_VALUE_BAND < edge < _VALUE_BAND:
                value_pct = round(edge, 1)
        out[sel] = {"best_odds": best_odds, "best_book": best_book,
                    "value_pct": value_pct, "fair_odds": fair_odds,
                    "fair_prob": round(prob, 4)}
    return out


def sharp_line(
    by_source: dict[str, dict[str, float]],
    *, sources: tuple[str, ...] = SHARP_SOURCES,
) -> dict[str, Any]:
    """One call: split the quotes into sharps vs books, blend the sharp fair,
    and value the books against it. Any source name not in ``sources`` is a
    bettable book.

    Returns {"fair": {...}, "sharp_sources": [...], "value": {sel: {...}},
    "book_count": n}."""
    sharps = {k: v for k, v in by_source.items() if k in sources}
    books = {k: v for k, v in by_source.items() if k not in sources}
    blended = blend_sharp(sharps, sources=sources)
    return {
        "fair": blended["fair"],
        "sharp_sources": blended["sources"],
        "value": book_value(books, blended["fair"]),
        "book_count": len(books),
    }
