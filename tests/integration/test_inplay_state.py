"""Live match state, and the in-play arb watch built on it.

The platform had no notion of a match being in progress: `prices` records change-points
but nothing distinguishes a live market from a pre-game one, and no scoreline was stored
anywhere. These cover the piece that fills that, and the first thing to depend on it.
"""

from __future__ import annotations

import datetime as dt

import pytest

from sportsdata_agents.data.models import MatchState
from sportsdata_agents.operations.monitoring import live_event_ids

pytestmark = pytest.mark.integration


def _state(session, event: str, status: str, *, minutes_ago: float, now: dt.datetime,
           home: int | None = None, away: int | None = None, clock: str | None = None):
    session.add(
        MatchState(
            captured_at=now - dt.timedelta(minutes=minutes_ago),
            provider="sportsbet", sport="afl", event_external_id=event,
            status=status, home_score=home, away_score=away, clock=clock,
        )
    )


async def test_only_events_reported_live_are_returned(session) -> None:
    now = dt.datetime.now(dt.UTC)
    _state(session, "running", "live", minutes_ago=1, now=now, home=44, away=38, clock="Q3 04:21")
    _state(session, "not_started", "pre", minutes_ago=1, now=now)
    _state(session, "finished", "ended", minutes_ago=1, now=now, home=91, away=88)
    await session.flush()

    assert await live_event_ids(session, now=now) == {"running"}


async def test_a_stale_live_row_does_not_count_as_live(session) -> None:
    """The failure this prevents: a `live` row from twenty minutes ago is evidence a match
    WAS running, not that it is. Acting on it fires an in-play alert on a finished match.
    """
    now = dt.datetime.now(dt.UTC)
    _state(session, "long_over", "live", minutes_ago=25, now=now)
    _state(session, "current", "live", minutes_ago=2, now=now)
    await session.flush()

    assert await live_event_ids(session, now=now, max_age_minutes=5.0) == {"current"}


async def test_the_latest_state_wins_not_any_state(session) -> None:
    """A match that just ended has a `live` row minutes old and an `ended` row seconds
    old. Both are inside the freshness window; only the newer one is true."""
    now = dt.datetime.now(dt.UTC)
    _state(session, "just_ended", "live", minutes_ago=3, now=now)
    _state(session, "just_ended", "ended", minutes_ago=0.5, now=now, home=2, away=1)
    await session.flush()

    assert await live_event_ids(session, now=now) == set()


async def test_a_suspended_match_is_not_live(session) -> None:
    """Suspension is the trap the in-play watch exists to avoid: one book freezes while
    the others move, and the frozen leg makes it look generous. `suspended` is tracked
    precisely so it is not mistaken for `live`."""
    now = dt.datetime.now(dt.UTC)
    _state(session, "var_check", "suspended", minutes_ago=1, now=now, home=1, away=1)
    await session.flush()

    assert await live_event_ids(session, now=now) == set()


async def test_the_clock_is_stored_as_the_provider_gave_it(session) -> None:
    """Every sport counts time differently — "Q3 04:21", "78'", "Set 2". A wrong
    normalisation is worse than the raw string a model can read."""
    now = dt.datetime.now(dt.UTC)
    _state(session, "e1", "live", minutes_ago=1, now=now, home=44, away=38, clock="Q3 04:21")
    await session.flush()

    row = (await session.execute(
        MatchState.__table__.select().where(MatchState.event_external_id == "e1")
    )).first()
    assert row is not None
    assert row.clock == "Q3 04:21"
    assert (row.home_score, row.away_score) == (44, 38)


async def test_a_score_that_has_not_moved_is_not_a_new_row(session) -> None:
    """Change-point shaped like `prices`: a row per poll would grow without bound while
    saying nothing, and the unique index is what makes a re-poll idempotent."""
    import sqlalchemy.exc

    now = dt.datetime.now(dt.UTC)
    at = now - dt.timedelta(minutes=1)
    for _ in range(2):
        session.add(
            MatchState(captured_at=at, provider="sportsbet", sport="afl",
                       event_external_id="dupe", status="live", home_score=1, away_score=0)
        )
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await session.flush()


def test_inplay_arb_is_a_registered_watch_kind() -> None:
    from sportsdata_agents.tools.monitoring import _KINDS

    assert "inplay_arb" in _KINDS


def test_the_inplay_watch_defaults_to_a_far_shorter_freshness_window() -> None:
    """Pre-game a twenty-minute-old price is usually still true; in-play it is fiction,
    and a cross-book sum with one stale leg is the most convincing fake arb there is.
    Pinned because the default is the safety property — nobody passes this param.
    """
    import inspect

    from sportsdata_agents.operations import monitoring

    src = inspect.getsource(monitoring._watch_inplay_arb)
    assert 'sub.params.get("max_age_minutes", 2.0)' in src, (
        "the in-play arb watch must default to a tight price-freshness window"
    )
    pre_game = inspect.getsource(monitoring._watch_arb)
    assert 'sub.params.get("max_age_minutes", 20.0)' in pre_game, (
        "if the pre-game default changed, revisit whether the in-play one still differs"
    )
