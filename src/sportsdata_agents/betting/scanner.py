"""Turning a cross-book comparison into candidate bets, with an honest edge number.

The comparator already does the hard part: it quotes the SAME combination at every book
that will price it. What this module adds is the question the policy needs answered —
**how good is the best price, as a number?** — and it is the easiest place in the whole
plane to fool yourself, so the reasoning is written down rather than assumed.

## Why the edge is measured against the other books

An SGM price cannot be de-vigged the ordinary way. De-vigging needs a complete market
whose outcomes sum to one; a single combination has no complementary set to normalise
against, so `quant.devig` and `quant.value` — both of which need a full market — do not
apply here.

What IS available is several books pricing the identical bet with their own correlation
models. Measured live, those models disagree by far more than the vig (correlation
adjustments ran from -41% to +5% on one anchor leg), so the dispersion is real signal
rather than noise. The consensus of the field is therefore the best available estimate of
the bet's worth, and a book paying well above it is the candidate.

**The consensus excludes the book being scored.** Otherwise a book that is out of line
drags the very number it is being compared against toward itself, which shrinks its own
apparent edge — the outlier partly hides itself.

**The median, not the mean.** One broken quote — a resolver matching the wrong leg, a
book returning a capped 1001.0 — moves a mean a long way and a median barely at all.

## Two bases, and why the difference matters

`relative` (the default) is `best_odds / consensus_odds - 1`: *how much more this book
pays for the identical bet*. It assumes only that the books carry broadly similar
margin, in which case the margin largely cancels in the ratio. It needs no invented
parameter, which is why it is the default — but it is **not** expected value, and a 3%
relative edge is not a 3% EV.

`ev` is `fair_probability * best_odds - 1`, the same definition `quant.value` uses. It is
the number people mean by "edge", but it needs a fair probability, and getting one from
vig-inclusive quotes means assuming an overround (`assumed_overround`). Feed it a wrong
assumption and every EV in the ledger is wrong in the same direction — silently, and
optimistically, because under-stating the overround over-states the edge.

**At `assumed_overround = 0` the two bases are the same number.** Not similar —
identical, algebraically: `(1/c) * o - 1` IS `o/c - 1`. So asking for `ev` without
supplying an overround gets you the relative figure wearing an EV label, which is the
precise mislabelling `edge_basis` exists to prevent. The EV basis only becomes a
different measurement once you tell it what margin the field is carrying, which is why
that case warns.

**The basis travels with every candidate** (`edge_basis`) and into the ledger, because a
ledger that mixes bases is a ledger whose numbers cannot be compared to each other.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from statistics import median
from typing import Any, Literal

log = logging.getLogger(__name__)

EdgeBasis = Literal["relative", "ev"]

#: Below this many books there is no field to compare against, so no edge can be
#: computed. Two is the bare minimum (one other book); three is the first count at which
#: a median means anything, and is the default the scanner recommends.
MIN_BOOKS_FOR_CONSENSUS = 2


@dataclass
class Candidate:
    """One book's price on one combination, scored against the field."""

    book: str
    fixture_id: str
    legs: list[dict]
    odds: float
    #: The field's view of this bet, excluding `book` itself.
    consensus_odds: float
    #: How much better than the field, in whatever `edge_basis` says.
    edge: float
    edge_basis: EdgeBasis
    #: How many books the consensus was built from (not counting this one).
    books_in_consensus: int
    #: The book's own quote id / token, where it issues one. Sportsbet's is short-lived.
    quote_id: str | None = None
    #: Whatever the quoter said about this price — capped payouts, collapsed legs.
    warnings: list[str] = field(default_factory=list)
    #: The quote as the book returned it, kept whole so the adapter can build a payload
    #: without re-quoting.
    quote: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"{self.book} {self.odds:.2f} on {len(self.legs)} legs "
            f"({self.edge:+.2%} {self.edge_basis} vs {self.consensus_odds:.2f} "
            f"from {self.books_in_consensus} books)"
        )


def _implied(odds: float) -> float:
    return 1.0 / odds


def consensus_of(others: list[float]) -> float:
    """The field's price, as decimal odds.

    Averaged in PROBABILITY space, not odds space. Odds are a reciprocal scale, so a
    mean of odds is pulled upward by long prices — averaging 2.0 and 10.0 gives 6.0,
    which implies a 16.7% chance, while the honest midpoint of a 50% and a 10% chance is
    30% (odds 3.33). Getting this backwards flatters every longshot in the book.
    """
    if not others:
        raise ValueError("no other books to form a consensus from")
    return 1.0 / median([_implied(o) for o in others])


def edge_of(
    *,
    odds: float,
    consensus_odds: float,
    basis: EdgeBasis = "relative",
    assumed_overround: float = 0.0,
) -> float:
    """Score one price against the field. See the module docstring for the two bases."""
    if odds <= 1.0 or consensus_odds <= 1.0:
        raise ValueError(f"not usable prices: odds={odds}, consensus={consensus_odds}")

    if basis == "relative":
        return odds / consensus_odds - 1.0

    # ev: turn the field's vig-inclusive implied probability into a fair one, then apply
    # the ordinary EV definition. `assumed_overround` of 0.0 means "the field's implied
    # probability IS fair", which is optimistic — it credits the whole margin as edge.
    if assumed_overround < 0:
        raise ValueError("assumed_overround cannot be negative")
    if assumed_overround == 0:
        log.warning(
            "EV edge computed with assumed_overround=0: this is ALGEBRAICALLY the same "
            "number as the 'relative' basis, labelled as EV. The field's margin is being "
            "counted as edge, which overstates every candidate. Set assumed_overround to "
            "the margin you believe the field carries, or use basis='relative' and call "
            "it what it is."
        )
    fair_prob = _implied(consensus_odds) / (1.0 + assumed_overround)
    return fair_prob * odds - 1.0


def candidates_from_comparison(
    comparison: dict[str, Any],
    *,
    fixture_id: str,
    basis: EdgeBasis = "relative",
    assumed_overround: float = 0.0,
    min_books: int = MIN_BOOKS_FOR_CONSENSUS,
    books: set[str] | None = None,
) -> list[Candidate]:
    """Score every priced book in one `sgm_books.compare()` result.

    Returns candidates sorted best edge first. A book is scored against the OTHERS, so
    with `min_books=2` a two-book comparison yields two candidates, each measured
    against the single other one.

    `books` optionally restricts which books become candidates — the rest still count
    toward the consensus, which is the right way round: a book you cannot place at is
    still evidence about what the bet is worth.
    """
    quotes = [q for q in comparison.get("quotes", []) if _usable(q)]
    if len(quotes) < min_books:
        return []

    legs = comparison.get("legs", [])
    by_book = {q["book"]: q for q in quotes}
    out: list[Candidate] = []

    for book, quote in by_book.items():
        if books is not None and book not in books:
            continue
        others = [q["book_odds"] for b, q in by_book.items() if b != book]
        if not others:
            continue
        consensus = consensus_of(others)
        try:
            edge = edge_of(
                odds=quote["book_odds"], consensus_odds=consensus,
                basis=basis, assumed_overround=assumed_overround,
            )
        except ValueError as exc:
            log.warning("skipping %s: %s", book, exc)
            continue
        out.append(Candidate(
            book=book,
            fixture_id=fixture_id,
            legs=legs,
            odds=quote["book_odds"],
            consensus_odds=round(consensus, 4),
            edge=edge,
            edge_basis=basis,
            books_in_consensus=len(others),
            quote_id=quote.get("quote_id"),
            warnings=list(quote.get("warnings", [])),
            quote=quote,
        ))

    return sorted(out, key=lambda c: c.edge, reverse=True)


def _usable(quote: dict[str, Any]) -> bool:
    """A quote that can take part in a consensus.

    Excludes the capped payout. Kambi stops moving a long multi at exactly 1001.0 — six,
    eight and fourteen legs all returned it on the verified fixture while the naive
    product kept climbing — so it is a ceiling, not a price, and letting it into a
    consensus would invent an edge for every other book on the board.
    """
    odds = quote.get("book_odds")
    if not isinstance(odds, int | float) or odds <= 1.0:
        return False
    if odds >= 1001.0:
        log.debug("ignoring capped quote from %s (%s)", quote.get("book"), odds)
        return False
    return True
