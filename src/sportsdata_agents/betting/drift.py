"""Re-price immediately before placing, and abandon the bet if the number moved.

The edge was computed against a quote. By the time the plane is ready to place, that
quote may be seconds or minutes old, and every book in the catalogue prices a multi at
placement time rather than honouring a quote id. Sportsbet and Entain go further: they
take a price the CLIENT asserts, so a stale number is not merely optimistic — it is
what gets sent.

So the rule is: fetch the price again, compare, and only place if it still supports the
bet. A bet placed at a worse price than the one that justified it is a different bet,
and usually a bad one.

## Which direction matters

Only movement AGAINST the bettor kills it. If the price drifted in your favour the edge
grew and there is nothing to protect against — abandoning there would be a bug that
quietly discards the best opportunities. The gate is therefore one-sided, and the test
suite pins that.

## Why the re-priced number is the one that gets sent

Never place at the price you computed the edge on; place at the price the book just
quoted. They are usually equal, and when they are not, the book's number is the real
one. `check()` returns it for exactly that reason.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DriftResult:
    ok: bool
    reason: str
    #: The price to actually place at — the freshly quoted one, always.
    price: float
    #: Signed fraction: negative means the price moved against the bettor.
    moved: float


def check(*, quoted: float, current: float, tolerance: float) -> DriftResult:
    """`quoted` is what the edge was computed on, `current` what the book says now.

    `tolerance` is a positive fraction (0.02 = 2%).
    """
    if quoted <= 1.0 or current <= 1.0:
        return DriftResult(False, f"not a usable price (quoted {quoted}, current {current})", current, 0.0)

    moved = (current - quoted) / quoted

    if moved >= 0:
        # Drifted out (or unchanged) — better for the bettor. Always fine.
        return DriftResult(True, f"price held or improved ({quoted:.3f} → {current:.3f})", current, moved)

    if abs(moved) > tolerance:
        return DriftResult(
            False,
            f"price shortened {abs(moved):.2%} ({quoted:.3f} → {current:.3f}), "
            f"beyond the {tolerance:.2%} tolerance — the edge that justified this bet is gone",
            current,
            moved,
        )
    return DriftResult(True, f"price moved {moved:.2%}, inside tolerance", current, moved)
