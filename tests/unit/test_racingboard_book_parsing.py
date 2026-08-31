"""The five parsing bugs the multi-book build turned up, pinned one by one.

Every one of them failed SILENTLY. None raised, none logged; each simply removed data
and left something that looked like a quiet racing day. That is the whole reason they
survived long enough to be found by measurement rather than by an error — and the
reason they need tests rather than a fix and a note.

Together they were the difference between the board covering half its races and
covering all of them.
"""

from __future__ import annotations

import pytest

from sportsdata_agents.interfaces.racingboard.corporate import (
    DABBLE_SPORT_TO_CODE,
    ENTAIN_CATEGORY_TO_CODE,
    PB_TYPE_TO_CODE,
    BookRace,
    CorporateBook,
    _epoch,
    _sb_code,
)
from sportsdata_agents.interfaces.racingboard.models import RaceRef
from sportsdata_agents.interfaces.racingboard.venues import venue_tokens

pytestmark = pytest.mark.unit

NOON = 1788139080.0


# ─── bug 1: Sportsbet sends an epoch integer, not ISO ───────────────────


def test_an_integer_start_time_parses() -> None:
    """Sportsbet's `startTime` is 1788145200 — an int. Parsing only ISO returned None
    for every Sportsbet race, and a race with no start cannot be told apart from the
    second meeting at the same track, so this alone cost a third of the coverage."""
    assert _epoch(1788145200) == 1788145200.0
    assert _epoch(1788145200.0) == 1788145200.0


def test_an_iso_start_time_still_parses() -> None:
    assert _epoch("2026-08-31T03:00:00Z") == pytest.approx(1788145200, abs=86400)
    assert _epoch("2026-08-31T03:00:00+00:00") is not None


def test_milliseconds_are_not_read_as_seconds() -> None:
    """A millisecond epoch read as seconds lands in the year 58,000 and matches nothing."""
    assert _epoch(1788145200000) == 1788145200.0


def test_a_missing_start_is_none_not_zero() -> None:
    """Zero would be a real timestamp (1970) and would match a race an epoch away."""
    for empty in (None, ""):
        assert _epoch(empty) is None
    assert _epoch("not a date") is None


# ─── bug 2: the venue normaliser ate month names ────────────────────────


def test_del_mar_survives_normalisation() -> None:
    """THE worst of the five. A bare `mar` was stripped as March, so `Del Mar` reduced
    to an EMPTY token set — and a venue that normalises to nothing matches nothing and
    is simply absent from the board. A major track, gone, in silence."""
    assert venue_tokens("Del Mar") == frozenset({"del", "mar"})


@pytest.mark.parametrize("name", ["May Park", "Augusta", "Marlborough", "Decatur"])
def test_other_month_shaped_names_survive_too(name: str) -> None:
    """`Del Mar` was the one that showed up on the day's card; it was not the only name
    the rule could have eaten."""
    assert venue_tokens(name), f"{name} normalised to nothing"


@pytest.mark.parametrize("name,expected", [
    ("Bathurst (AUS) 8th Jul", "bathurst"),
    ("Bathurst 8 Jul", "bathurst"),
    ("Bathurst Jul 8", "bathurst"),
])
def test_a_real_date_is_still_stripped(name: str, expected: str) -> None:
    """The fix narrows the rule to dates; it must not stop removing them."""
    assert venue_tokens(name) == frozenset({expected})


# ─── bug 3: the Ladbrokes category ids ──────────────────────────────────


def test_all_three_racing_codes_have_a_category() -> None:
    """Two of the three UUIDs were originally copied from a TRUNCATED display and were
    wrong, so greyhound and harness mapped to no code and every one of their races was
    dropped without a word. Read off the live payload and identified by the meetings
    behind each: Corowa -> R, Sandown Park -> G, Solvalla -> H."""
    assert set(ENTAIN_CATEGORY_TO_CODE.values()) == {"R", "G", "H"}
    for cid in ENTAIN_CATEGORY_TO_CODE:
        assert len(cid) == 36 and cid.count("-") == 4, f"{cid} is not a full UUID"


def test_every_book_maps_all_three_codes() -> None:
    """A code missing from any book's mapping is a whole discipline silently absent from
    that book's column."""
    assert set(PB_TYPE_TO_CODE.values()) == {"R", "G", "H"}
    assert set(DABBLE_SPORT_TO_CODE.values()) == {"R", "G", "H"}
    # Sportsbet splits each code across several className values.
    assert _sb_code("Horses - Aus/NZ") == "R"
    assert _sb_code("Horses - International") == "R"
    assert _sb_code("Horses - Asia") == "R"
    assert _sb_code("Greyhound Racing") == "G"
    assert _sb_code("Harness Racing") == "H"
    assert _sb_code("Harness Racing - International") == "H"


# ─── bug 4: a lone candidate is not automatically the match ─────────────


def _book(races: list[BookRace]) -> CorporateBook:
    b = CorporateBook()
    b.races = races
    return b


def test_a_single_candidate_still_has_to_match_the_clock() -> None:
    """`by_start` returned a lone candidate without checking the tolerance. A book that
    also lists TOMORROW has exactly one race 8 at Northfield Park once today's has run,
    so the board would have priced a race twenty hours away and looked normal doing it.

    Caught by a test rather than by the live run, which is the argument for the test:
    the live board would simply have shown a plausible wrong number.
    """
    b = _book([BookRace("H", "Northfield Park", 8, NOON + 20 * 3600, "tomorrow")])
    ref = RaceRef(race_key="k", code="H", venue="Northfield Park", venue_mnem=None,
                  race_no=8, race_name="", start_time="", date="2026-08-31",
                  start_epoch=NOON)
    assert b.handle_for(ref) is None


def test_a_candidate_without_a_published_start_is_still_accepted() -> None:
    """The tolerance check must not punish a book that simply publishes no time: the
    race number already matched, and refusing here would trade a wrong price for a
    missing one that did not need to be missing."""
    b = _book([BookRace("H", "Northfield Park", 8, None, "untimed")])
    ref = RaceRef(race_key="k", code="H", venue="Northfield Park", venue_mnem=None,
                  race_no=8, race_name="", start_time="", date="2026-08-31",
                  start_epoch=NOON)
    assert b.handle_for(ref) == "untimed"


# ─── bug 5: Dabble must price even though it cannot discover ────────────


def test_dabble_prices_a_race_it_could_not_have_discovered() -> None:
    """The accepted trade: Dabble publishes no race number, so it contributes no races
    to the spine — but it must still price the ones the others found, or adding it buys
    nothing. This is the join that makes that true."""
    b = _book([BookRace("G", "Sandown Park", None, NOON, "dabble-fixture")])
    b.name = "dabble"
    ref = RaceRef(race_key="k", code="G", venue="Sandown Park", venue_mnem=None,
                  race_no=4, race_name="", start_time="", date="2026-08-31",
                  start_epoch=NOON)
    assert b.handle_for(ref) == "dabble-fixture"
