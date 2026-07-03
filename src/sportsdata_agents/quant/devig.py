"""De-vig methods: proportional and the piecewise overround curve.

Proportional de-vig (divide every implied probability by the board sum) is
robust but wrong in the tails: books load margin onto longshots and cannot
push a near-certainty past 1.0, so proportional removal overstates longshot
fair probability and butchers odds-on quotes (a 1.07 place quote is NOT
carrying 25%+ margin — its marked probability simply has no room).

The piecewise curve models how books actually shape margin:

- longshot tail (below ``low_break``): margin grows proportionally with p;
- body: flat ``max_margin``;
- favourite tail (above ``high_break``): margin compressed into the
  remaining headroom so the marked value never passes 1.

``piecewise_fair_probabilities`` inverts that shape per selection and
renormalises the residual. Shape parameters come from fitting a book's
boards over history; the defaults are a conservative generic shape. This is
standard bookmaking maths — see e.g. the overround-removal literature.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["OverroundCurve", "piecewise_fair_probabilities", "proportional_fair_probabilities"]


@dataclass(frozen=True)
class OverroundCurve:
    """Piecewise margin shape: floor margin, body margin, and the two breaks."""

    min_margin: float = 0.01
    max_margin: float = 0.05
    low_break: float = 0.10
    high_break: float = 0.80

    def __post_init__(self) -> None:
        if not 0.0 <= self.min_margin <= self.max_margin:
            raise ValueError("require 0 <= min_margin <= max_margin")
        if not 0.0 < self.low_break < self.high_break < 1.0:
            raise ValueError("require 0 < low_break < high_break < 1")
        if self.max_margin - self.min_margin >= 1.0 - self.high_break:
            raise ValueError("require max_margin - min_margin < 1 - high_break (monotone favourite tail)")

    def remove(self, marked: float) -> float:
        """Fair probability for a marked (margin-included) probability."""
        if not 0.0 <= marked <= 1.0:
            raise ValueError(f"marked probability must be in [0, 1], got {marked}")
        if marked < 1e-12:
            return marked
        ramp = self.max_margin - self.min_margin
        if marked < self.low_break + self.max_margin:
            return max(0.0, (marked - self.min_margin) * self.low_break / (self.low_break + ramp))
        if marked <= self.high_break + self.max_margin:
            return marked - self.max_margin
        numerator = (marked - self.min_margin) * (1.0 - self.high_break) - ramp
        denominator = 1.0 - self.high_break - ramp
        return numerator / denominator


def proportional_fair_probabilities(odds: Mapping[str, float]) -> dict[str, float]:
    """The classic de-vig: implied probabilities scaled to sum to one."""
    implied = _implied(odds)
    total = sum(implied.values())
    if total <= 1.0:
        raise ValueError(f"market sums to {total:.3f} — incomplete or arbed; pass the full market")
    return {name: p / total for name, p in implied.items()}


def piecewise_fair_probabilities(
    odds: Mapping[str, float], curve: OverroundCurve | None = None
) -> dict[str, float]:
    """Curve-based de-vig: invert the margin shape per selection, renormalise.

    Falls back to proportional for the whole market when the curve floors any
    selection to zero (a fair probability of zero is unusable downstream).
    """
    shape = curve if curve is not None else OverroundCurve()
    implied = _implied(odds)
    if sum(implied.values()) <= 1.0:
        raise ValueError("market sums under 1 — incomplete or arbed; pass the full market")
    fair = {name: shape.remove(p) for name, p in implied.items()}
    if any(value <= 0.0 for value in fair.values()):
        return proportional_fair_probabilities(odds)
    total = sum(fair.values())
    return {name: value / total for name, value in fair.items()}


def _implied(odds: Mapping[str, float]) -> dict[str, float]:
    if len(odds) < 2:
        raise ValueError("a market needs at least two priced selections")
    for name, quote in odds.items():
        if quote < 1.01:
            raise ValueError(f"odds {quote} for {name!r} below 1.01")
    return {name: 1.0 / quote for name, quote in odds.items()}
