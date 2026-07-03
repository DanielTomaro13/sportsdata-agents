"""The action layer: turn value candidates into a ranked, risk-aware board.

An edge with no path to action is a number on a screen. This module ranks
candidates by ``edge x confidence x freshness`` and annotates correlated
exposure — always advisory, the platform never places bets.

- **confidence** — how far the model-book gap sits outside the model's own
  error band: ``min(1, gap / (3 x std_error))``; candidates without an error
  bar get a conservative 0.5 (unknown certainty is not full certainty).
- **freshness** — exponential decay on quote age with a configurable
  half-life (default 15 minutes): yesterday's edge is not an edge.
- **correlated exposure** — candidates on the same event move together;
  every row carries its event's total candidate count and combined score so
  a stack of "independent" bets on one game is visible for what it is.
"""

from __future__ import annotations

import math
from typing import Any

__all__ = ["rank_value_board"]


def rank_value_board(
    candidates: list[dict[str, Any]],
    *,
    freshness_half_life_minutes: float = 15.0,
    top: int = 20,
) -> dict[str, Any]:
    """Rank candidates: each needs ``edge_pct``; optional ``std_error``,
    ``model_prob``, ``odds``, ``age_minutes``, ``event_external_id``."""
    if freshness_half_life_minutes <= 0.0:
        raise ValueError("freshness_half_life_minutes must be positive")
    if top < 1:
        raise ValueError("top must be at least 1")
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        edge_pct = float(candidate["edge_pct"])
        if edge_pct <= 0.0:
            continue
        std_error = candidate.get("std_error")
        model_prob = candidate.get("model_prob")
        odds = candidate.get("odds")
        if std_error is not None and model_prob is not None and odds is not None and float(odds) > 0:
            gap = abs(float(model_prob) - 1.0 / float(odds))
            confidence = min(1.0, gap / (3.0 * float(std_error))) if float(std_error) > 0 else 1.0
        else:
            confidence = 0.5  # unknown certainty is not full certainty
        age = float(candidate.get("age_minutes", 0.0))
        freshness = math.exp(-math.log(2.0) * age / freshness_half_life_minutes)
        rows.append(
            {
                **candidate,
                "confidence": round(confidence, 4),
                "freshness": round(freshness, 4),
                "score": round(edge_pct * confidence * freshness, 4),
            }
        )
    rows.sort(key=lambda r: -r["score"])
    rows = rows[:top]

    by_event: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_event.setdefault(str(row.get("event_external_id", "?")), []).append(row)
    for group in by_event.values():
        combined = round(sum(r["score"] for r in group), 4)
        for row in group:
            row["event_candidates"] = len(group)
            row["event_combined_score"] = combined
    correlated = sorted(
        (
            {"event_external_id": event_id, "candidates": len(group),
             "combined_score": round(sum(r["score"] for r in group), 4)}
            for event_id, group in by_event.items()
            if len(group) > 1
        ),
        key=lambda g: -g["combined_score"],
    )
    return {"board": rows, "correlated_exposure": correlated}
