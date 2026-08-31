"""Venue and runner matching — the join key the whole racing board depends on.

None of these failures raise anything. A venue that stops matching just removes that
book's column, which looks like a quiet day rather than a regression; a venue that
matches too eagerly shows a price from the wrong race, which looks like a price. So
both directions need pinning, and the over-matching direction matters more, because a
gap is visible and a wrong number is not.
"""

from __future__ import annotations

import pytest

from sportsdata_agents.interfaces.racingboard.venues import (
    norm_runner,
    norm_venue,
    unique_venue_match,
    venue_tokens,
)

pytestmark = pytest.mark.unit


# ─── the case the old matcher could not reach ───────────────────────────


def test_a_short_name_reaches_its_long_form() -> None:
    """THE motivating failure. `_venue_compatible` was exact-or-5-char-PREFIX, and
    `MOHAWK` is not a prefix of `Woodbine Mohawk Park`, so every harness race at the
    track went uncovered — measured as 1-of-11 covered while the books had all of it."""
    assert unique_venue_match("MOHAWK", [("Woodbine Mohawk Park", "hit")]) == "hit"


@pytest.mark.parametrize("board,book", [
    ("NORTHFIELD PARK", "Northfield Pk"),
    ("SARATOGA", "Saratoga TB"),
    ("RICCARTON", "Riccarton Park"),
    ("Bathurst (AUS)", "Bathurst"),
    ("Prairie Medw", "Prairie Meadows"),
    ("CLUB HIPICO DE SANTIAGO", "Santiago"),
])
def test_the_spellings_books_actually_use(board: str, book: str) -> None:
    """Every pair here was taken off a live card, not invented."""
    assert unique_venue_match(board, [(book, "hit")]) == "hit"


# ─── and the case it must never reach ───────────────────────────────────


def test_woodbine_is_not_woodbine_mohawk_park() -> None:
    """Two DIFFERENT tracks — one thoroughbred, one harness. Merging them shows a price
    from the wrong race, the same way a Women's fixture merging with the men's once
    manufactured a 74% arbitrage. `Woodbine` must take the exact match and never be
    captured by the longer name, even though its tokens are a subset of it."""
    candidates = [("Woodbine", "W"), ("Woodbine Mohawk Park", "WMP")]
    assert unique_venue_match("Woodbine", candidates) == "W"
    assert unique_venue_match("WOODBINE", candidates) == "W"
    # and from the other side
    assert unique_venue_match("Woodbine Mohawk Park", candidates) == "WMP"


def test_ambiguity_refuses_rather_than_guesses() -> None:
    """When two candidates could both contain the name, the answer is no answer."""
    assert unique_venue_match("Sandown", [
        ("Sandown Park Extra", "a"), ("Sandown Lakeside Downs", "b"),
    ]) is None


def test_an_unrelated_track_never_matches() -> None:
    assert unique_venue_match("BATHURST", [("Ballarat", "x"), ("Bendigo", "y")]) is None


def test_an_empty_name_matches_nothing() -> None:
    """A book sending a blank venue must not silently match the first candidate."""
    assert unique_venue_match("", [("Bathurst", "x")]) is None
    assert unique_venue_match("   ", [("Bathurst", "x")]) is None


# ─── why generic words are dropped rather than tolerated ────────────────


def test_generic_words_make_names_EQUAL_not_merely_compatible() -> None:
    """Load-bearing. `RICCARTON` and `Riccarton Park` reduce to the same token set, so
    they take the EXACT path and never enter the ambiguous subset path — which means a
    third track sharing the stem cannot make a settled pair suddenly ambiguous."""
    assert venue_tokens("RICCARTON") == venue_tokens("Riccarton Park")
    assert norm_venue("Northfield Pk") == norm_venue("NORTHFIELD PARK")


def test_a_discipline_suffix_does_not_split_a_track() -> None:
    assert venue_tokens("Saratoga") == venue_tokens("Saratoga TB")


# ─── runners ────────────────────────────────────────────────────────────


def test_a_country_suffix_does_not_split_a_runner() -> None:
    """The asymmetry that was there: `_norm_venue` cut at '(' and `_norm_runner` did
    not, so `Jadzia (NZ)` reduced to `jadzianz` and never met `Jadzia`. Country tags are
    a convention on IMPORTED runners — precisely the international coverage the union
    spine adds — so it would have bitten exactly when the spine widened."""
    assert norm_runner("Jadzia (NZ)") == norm_runner("Jadzia")
    assert norm_runner("Mr Money Bags (IRE)") == norm_runner("Mr Money Bags")


def test_a_saddlecloth_number_does_not_split_a_runner() -> None:
    assert norm_runner("1. Chix Diggus") == norm_runner("CHIX DIGGUS")
    assert norm_runner("12) Paw Archie") == norm_runner("Paw Archie")


def test_punctuation_does_not_split_a_runner() -> None:
    assert norm_runner("Don't Doubt Tigga") == norm_runner("DONT DOUBT TIGGA")


def test_two_different_runners_stay_different() -> None:
    assert norm_runner("Blue Suede Shoes") != norm_runner("Blue Suede Shoe")
