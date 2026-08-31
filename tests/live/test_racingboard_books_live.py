"""Every book still indexes races, with codes and start times — against the live feeds.

The unit tests pin the parsing once the shape is known. They cannot catch the failure
that actually happened, which is the FIELD NAME being wrong: Ladbrokes' race number is
`number` and reading `race_number` indexed zero races, while Sportsbet's `startTime` is
an epoch integer and parsing it as ISO gave every race a start of None. Both look
exactly like a book with nothing on today.

So this is the contract check, and it runs against the real feeds:

    pytest -m live tests/live/test_racingboard_books_live.py

Not in CI — it needs an AU IP and the day's card. Run it when a book's coverage drops
without an obvious cause, and after any change to a book's `build_index`.
"""

from __future__ import annotations

import datetime as dt

import pytest

from sportsdata_agents.interfaces.racingboard.corporate import (
    CorporateSource,
    TabBook,
    build_books,
)
from sportsdata_agents.interfaces.racingboard.engine import SportsDataEngine
from sportsdata_agents.interfaces.racingboard.spine import cluster_races

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

#: Below this a book is not "having a quiet day", it is broken. The smallest real card
#: measured on any book was TAB's 413; a genuine off-day is still in the hundreds.
MIN_RACES = 50


@pytest.fixture(scope="module")
async def indexed():
    engine = SportsDataEngine()
    books = [TabBook(), *build_books()]
    await CorporateSource(books=books).refresh_indices(engine, dt.date.today().isoformat())
    return books


async def test_every_book_indexes_a_real_card(indexed) -> None:
    """THE guard. A field rename upstream drops a book to zero in silence — which is
    precisely what `race_number` vs `number` did to Ladbrokes."""
    empty = [b.name for b in indexed if len(b.races) < MIN_RACES]
    assert not empty, (
        f"books indexing under {MIN_RACES} races: "
        f"{ {b.name: len(b.races) for b in indexed if b.name in empty} } — "
        "check the race-id and race-number field names against the live payload")


async def test_every_book_carries_start_times(indexed) -> None:
    """A race with no start cannot be told apart from the second meeting at the same
    track, so a start-time field rename silently halves that book's coverage rather
    than removing it — the harder failure to notice of the two."""
    for b in indexed:
        if not b.races:
            continue
        timed = sum(1 for r in b.races if r.start is not None)
        assert timed / len(b.races) >= 0.95, (
            f"{b.name}: only {timed}/{len(b.races)} races have a start time — "
            "the start field has probably been renamed or changed type")


async def test_every_book_carries_more_than_one_code(indexed) -> None:
    """A code mapping that stops matching drops a whole discipline. Dabble is included:
    it carries all three, via `sportName` on the active-competitions feed."""
    for b in indexed:
        codes = {r.code for r in b.races}
        assert len(codes) >= 2, f"{b.name} only has codes {codes} — check its code mapping"


async def test_the_union_spine_is_wider_than_any_single_book(indexed) -> None:
    """The reason the spine exists. If the union is no bigger than its largest member,
    either discovery has collapsed back onto one book or the clustering is over-merging.
    """
    spine = cluster_races([(b.name, br) for b in indexed for br in b.races])
    biggest = max(len(b.races) for b in indexed)
    assert len(spine) > biggest * 1.05, (
        f"union spine is {len(spine)} races against a largest single book of {biggest} — "
        "discovery has collapsed onto one book, or clustering is merging distinct races")


async def test_tab_is_not_the_ceiling(indexed) -> None:
    """The specific regression: races the old TAB-only spine could never show."""
    spine = cluster_races([(b.name, br) for b in indexed for br in b.races])
    without_tab = [r for r in spine if "tab" not in r.books]
    assert len(without_tab) > 100, (
        f"only {len(without_tab)} races come from books other than TAB — "
        "the spine has regressed towards TAB-only")
