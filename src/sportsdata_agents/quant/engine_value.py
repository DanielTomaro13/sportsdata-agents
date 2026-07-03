"""Consistency-edge scan: a book's derivative quotes vs engine fair prices.

The consistency edge: calibrate an engine to a book's own anchor markets
(h2h/total), price the derivative surface, and flag the book's OTHER quotes
that disagree with their own anchors. Pure maths here — quote gathering and
engine calls live with the callers.

Noise discipline is enforced, not advisory: an engine price carries a Monte
Carlo ``std_error``, and a candidate only counts when its edge clears
``min_edge_pct`` AND the model-vs-book probability gap exceeds
``error_multiple`` standard errors. Differences inside the error band are
noise, never edge.
"""

from __future__ import annotations

from typing import Any

__all__ = ["consistency_scan"]


def consistency_scan(
    quotes: list[dict[str, Any]],
    engine_prices: list[dict[str, Any]],
    *,
    min_edge_pct: float = 2.0,
    error_multiple: float = 3.0,
) -> dict[str, Any]:
    """Match book quotes to engine prices and rank the disagreements.

    ``quotes`` rows: {market, selection, line?, odds}. ``engine_prices``
    rows: {market, selection, line?, fair_probability, std_error?}. Rows are
    joined on (market, selection, line). Returns {candidates: [...],
    checked: n, skipped_noise: n} with candidates sorted by edge descending.
    """
    if min_edge_pct < 0.0 or error_multiple < 0.0:
        raise ValueError("min_edge_pct and error_multiple must be non-negative")
    fair_by_key: dict[tuple[str, str, float | None], tuple[float, float | None]] = {}
    for row in engine_prices:
        p = float(row["fair_probability"])
        if not 0.0 <= p <= 1.0:
            raise ValueError(f"fair_probability {p} outside [0, 1]")
        err = row.get("std_error")
        key = (str(row["market"]), str(row["selection"]), _line(row.get("line")))
        fair_by_key[key] = (p, float(err) if err is not None else None)

    candidates: list[dict[str, Any]] = []
    checked = 0
    skipped_noise = 0
    for quote in quotes:
        odds = float(quote["odds"])
        if odds < 1.01:
            raise ValueError(f"odds {odds} below 1.01 for {quote.get('selection')!r}")
        key = (str(quote["market"]), str(quote["selection"]), _line(quote.get("line")))
        found = fair_by_key.get(key)
        if found is None:
            continue
        checked += 1
        fair_probability, std_error = found
        edge_pct = (fair_probability * odds - 1.0) * 100.0
        if edge_pct < min_edge_pct:
            continue
        # noise gate: the model-book probability gap must clear the error band
        if std_error is not None:
            gap = abs(fair_probability - 1.0 / odds)
            if gap <= error_multiple * std_error:
                skipped_noise += 1
                continue
        candidates.append(
            {
                "market": key[0],
                "selection": key[1],
                "line": key[2],
                "odds": odds,
                "model_prob": round(fair_probability, 4),
                "model_fair_odds": round(1.0 / fair_probability, 3) if fair_probability > 0 else None,
                "edge_pct": round(edge_pct, 2),
                "std_error": std_error,
            }
        )
    candidates.sort(key=lambda c: -c["edge_pct"])
    return {
        "min_edge_pct": min_edge_pct,
        "error_multiple": error_multiple,
        "checked": checked,
        "skipped_noise": skipped_noise,
        "candidates": candidates,
    }


def _line(value: Any) -> float | None:
    return None if value is None else float(value)
