"""The union spine, and the book lookups that hang off it.

The board used to build its race list from TAB alone — 413 races against the ~1,120 the
five books have between them — so a race TAB did not carry could never appear no matter
how many books priced it. These pin the assembly and, more importantly, the two ways it
can go wrong in silence: a race that fails to join (a book's column quietly empties) and
two races that wrongly join (a price shown against the wrong runners).
"""

from __future__ import annotations

import pytest

from sportsdata_agents.interfaces.racingboard.corporate import BookRace, CorporateBook
from sportsdata_agents.interfaces.racingboard.models import RaceRef
from sportsdata_agents.interfaces.racingboard.spine import cluster_races

pytestmark = pytest.mark.unit

NOON = 1788139080.0
HOUR = 3600.0


def ref(code="H", venue="Northfield Park", race_no=8, start=NOON) -> RaceRef:
    return RaceRef(race_key="k", code=code, venue=venue, venue_mnem=None,
                   race_no=race_no, race_name="", start_time="", date="2026-08-31",
                   start_epoch=start)


def book(races: list[BookRace], name="testbook") -> CorporateBook:
    b = CorporateBook()
    b.name = name
    b.races = races
    return b


# ─── the union ──────────────────────────────────────────────────────────


def test_books_spelling_a_venue_differently_make_ONE_race() -> None:
    races = cluster_races([
        ("tab", BookRace("H", "NORTHFIELD PARK", 8, NOON, "t")),
        ("pointsbet", BookRace("H", "Northfield Pk", 8, NOON, "p")),
        ("ladbrokes", BookRace("H", "Northfield Park", 8, NOON, "l")),
    ])
    assert len(races) == 1
    assert races[0].books == ["ladbrokes", "pointsbet", "tab"]


def test_a_race_no_book_but_one_has_still_enters_the_spine() -> None:
    """The entire point of the union: TAB is a contributor, not the gate."""
    races = cluster_races([("pointsbet", BookRace("R", "Del Mar", 3, NOON, "p"))])
    assert len(races) == 1
    assert races[0].books == ["pointsbet"]
    assert races[0].venue_mnem is None      # TAB has no handle for it


def test_the_display_name_is_the_most_informative_spelling() -> None:
    races = cluster_races([
        ("tab", BookRace("H", "MOHAWK", 1, NOON, "t")),
        ("ladbrokes", BookRace("H", "Woodbine Mohawk Park", 1, NOON, "l")),
    ])
    assert races[0].venue == "Woodbine Mohawk Park"


def test_different_codes_never_merge() -> None:
    """`Woodbine` (thoroughbred) and `Woodbine Mohawk Park` (harness) are different
    tracks, and the code gate separates them before the names are even considered."""
    races = cluster_races([
        ("tab", BookRace("R", "Woodbine", 1, NOON, "t")),
        ("tab", BookRace("H", "Woodbine Mohawk Park", 1, NOON, "t2")),
    ])
    assert len(races) == 2


# ─── two meetings at one track ──────────────────────────────────────────


def test_a_day_and_a_night_card_are_two_races_not_one() -> None:
    """THE defect this exists for. Townsville ran a day AND a night greyhound meeting,
    each with its own race 1, and so did Woodbine Mohawk Park — PointsBet carried 121
    such pairs, Sportsbet 20. Keyed on (code, venue, race_no) alone the two folded into
    one race, which would show one card's prices against the other's runners."""
    races = cluster_races([
        ("pointsbet", BookRace("G", "Townsville", 1, NOON, "day")),
        ("pointsbet", BookRace("G", "Townsville", 1, NOON + 8 * HOUR, "night")),
    ])
    assert len(races) == 2
    assert {r.start_epoch for r in races} == {NOON, NOON + 8 * HOUR}


def test_the_lookup_picks_the_right_one_of_the_two() -> None:
    """And the same pair must resolve by start on the way back out. Refusing here was
    what dropped PointsBet — the biggest catalogue of the five — to half the board."""
    b = book([
        BookRace("G", "Townsville", 1, NOON, "day"),
        BookRace("G", "Townsville", 1, NOON + 8 * HOUR, "night"),
    ])
    assert b.handle_for(ref(code="G", venue="TOWNSVILLE", race_no=1, start=NOON)) == "day"
    assert b.handle_for(
        ref(code="G", venue="TOWNSVILLE", race_no=1, start=NOON + 8 * HOUR)) == "night"


def test_tomorrows_meeting_is_not_todays_race() -> None:
    """A book listing tomorrow's card too must not answer for today. Northfield Park's
    R8 exists on both days; matching the wrong one prices a race 20 hours away."""
    b = book([BookRace("H", "Northfield Park", 8, NOON + 20 * HOUR, "tomorrow")])
    assert b.handle_for(ref(start=NOON)) is None


# ─── books that publish no race number ──────────────────────────────────


def test_a_book_without_race_numbers_joins_on_start_time() -> None:
    """Dabble publishes a race NAME and an advertisedStart and nothing numbering them,
    so a spine keyed on race_no alone would have excluded it entirely — 137 meetings,
    more than any other book, contributing nothing."""
    b = book([BookRace("R", "Ballarat Synthetic", None, NOON, "dab")], name="dabble")
    assert b.handle_for(ref(code="R", venue="Ballarat Synthetic", race_no=1)) == "dab"


def test_an_unnumbered_book_still_respects_the_clock() -> None:
    b = book([BookRace("R", "Ballarat Synthetic", None, NOON + HOUR, "later")], name="dabble")
    assert b.handle_for(ref(code="R", venue="Ballarat Synthetic", race_no=1)) is None


def test_unnumbered_races_do_not_enter_the_spine() -> None:
    """They cannot be numbered, and a board row with no race number is not usable — so
    such a book prices races the others discovered rather than discovering its own."""
    assert cluster_races([("dabble", BookRace("R", "Ballarat", None, NOON, "d"))]) == []


# ─── refusing ───────────────────────────────────────────────────────────


def test_a_book_that_does_not_have_the_race_returns_nothing() -> None:
    b = book([BookRace("R", "Ballarat", 1, NOON, "x")])
    assert b.handle_for(ref(code="R", venue="Bendigo", race_no=1)) is None
    assert b.handle_for(ref(code="G", venue="Ballarat", race_no=1)) is None   # wrong code
    assert b.handle_for(ref(code="R", venue="Ballarat", race_no=9)) is None   # wrong race
