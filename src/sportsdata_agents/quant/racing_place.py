"""Textbook win-to-place conversion for racing (Harville 1973).

Given each runner's win probability, the probability the field finishes in
any exact order follows by conditioning: P(j second | i first) is j's win
probability renormalised over everyone except i, and so on down the order.
Place (top-k) probabilities are sums over those orderings.

This is the published, uncalibrated model: it systematically overrates
favourites deeper in the finishing order (long documented in the racing
literature). It is still far better than guessing, and it is free. The
place probabilities here are exact enumerations for k <= 3 over fields of
any size, vectorised in plain Python (fields are small).
"""

from __future__ import annotations

from collections.abc import Mapping

__all__ = ["harville_place_probabilities"]


def harville_place_probabilities(win: Mapping[str, float], places: int) -> dict[str, float]:
    """P(runner finishes in the first ``places``) under the Harville model.

    ``win`` maps runner name to win probability (renormalised on entry;
    strictly positive). ``places`` may be 1..3 (the standard place terms);
    a ``places`` >= field size returns certainty for everyone.
    """
    if len(win) < 2:
        raise ValueError("a race needs at least two runners")
    if not 1 <= places <= 3:
        raise ValueError(f"places must be between 1 and 3, got {places}")
    values = list(win.values())
    if any(v <= 0.0 for v in values):
        raise ValueError("win probabilities must be strictly positive (drop scratched runners)")
    total = sum(values)
    names = list(win)
    p = [v / total for v in values]
    n = len(p)
    if places >= n:
        return dict.fromkeys(names, 1.0)

    top = list(p)  # k = 1
    if places >= 2:
        second = [0.0] * n
        for i in range(n):
            for j in range(n):
                if i != j:
                    second[j] += p[i] * p[j] / (1.0 - p[i])
        top = [a + b for a, b in zip(top, second, strict=True)]
    if places == 3:
        third = [0.0] * n
        for i in range(n):
            for j in range(n):
                if j == i:
                    continue
                pair = p[i] * p[j] / (1.0 - p[i])
                remaining = 1.0 - p[i] - p[j]
                for k in range(n):
                    if k != i and k != j:
                        third[k] += pair * p[k] / remaining
        top = [a + b for a, b in zip(top, third, strict=True)]
    return dict(zip(names, top, strict=True))
