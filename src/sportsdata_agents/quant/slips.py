"""Bet-slip advisory maths: fair cash-out and redundancy warnings.

Industry-standard, model-free calculations (advisory only — the platform
never places or cashes out bets):

- **Cash-out**: an open position paying ``potential_payout`` with current
  win probability *p* is worth ``p x payout``; books shade that by a
  cash-out margin. Comparing their offer to the fair number is the point.
- **Redundancy**: two legs on the same market/line either duplicate each
  other (same selection — doubled risk, no new information) or oppose each
  other (different selections in a single-winner market — the multi can
  never pay both). Multi-winner markets (each-way, top-N) are exempt: pass
  ``single_winner=False`` legs accordingly.
"""

from __future__ import annotations

from typing import Any

__all__ = ["cash_out_value", "redundant_legs"]


def cash_out_value(win_probability: float, potential_payout: float, margin: float = 0.0) -> dict[str, Any]:
    """Fair value of an open position, optionally shaded by a book margin."""
    if not 0.0 <= win_probability <= 1.0:
        raise ValueError(f"win_probability must be in [0, 1], got {win_probability}")
    if potential_payout < 0.0:
        raise ValueError(f"potential_payout cannot be negative, got {potential_payout}")
    if not 0.0 <= margin <= 1.0:
        raise ValueError(f"margin must be in [0, 1], got {margin}")
    fair = win_probability * potential_payout
    return {
        "fair_value": round(fair, 4),
        "shaded_value": round(fair * (1.0 - margin), 4),
        "win_probability": win_probability,
        "potential_payout": potential_payout,
        "margin": margin,
    }


def redundant_legs(legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pairs of legs that should not share a slip, with a reason.

    Legs: ``{market, selection, line?, single_winner?}`` (``single_winner``
    defaults True). Returns ``[{first, second, reason}]`` where reason is
    ``duplicate`` or ``opposed``.
    """
    def _line(leg: dict[str, Any]) -> float | None:
        raw = leg.get("line")
        if raw is None:
            return None
        try:
            return float(raw)  # "1.5" and 1.5 must compare equal
        except (TypeError, ValueError):
            return None

    if len(legs) > 200:
        raise ValueError(f"too many legs ({len(legs)}) — redundancy check is quadratic; cap is 200")
    flagged: list[dict[str, Any]] = []
    for i, a in enumerate(legs):
        for b in legs[i + 1 :]:
            same_market = a["market"] == b["market"] and _line(a) == _line(b)
            if not same_market:
                continue
            if a["selection"] == b["selection"]:
                flagged.append({"first": a, "second": b, "reason": "duplicate"})
            elif a.get("single_winner", True) and b.get("single_winner", True):
                flagged.append({"first": a, "second": b, "reason": "opposed"})
    return flagged
