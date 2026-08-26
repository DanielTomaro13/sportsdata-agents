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


async def test_the_inplay_watch_asks_the_scanner_for_started_events(session, monkeypatch) -> None:
    """THE BUG THIS EXISTS FOR. `scan_arbs` defaults `min_lead_minutes` to +15 and skips
    any fixture starting inside that window — correct for the pre-game watch, fatal here.
    Shipped without the override, this function filtered FOR live events and then called a
    scanner that filtered them straight back out, so it could never fire.

    The first version of these tests checked `live_event_ids` and the kind registration —
    the parts — and both passed while the watch was dead. This checks the contract
    between them instead.
    """
    from sportsdata_agents.data.models import Subscription
    from sportsdata_agents.operations import monitoring

    now = dt.datetime.now(dt.UTC)
    _state(session, "live_one", "live", minutes_ago=1, now=now, home=1, away=0)
    await session.flush()

    seen: dict[str, object] = {}

    async def _fake_scan(_session, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr("sportsdata_agents.quant.arbitrage.scan_arbs", _fake_scan)
    sub = Subscription(name="t", kind="inplay_arb", params={}, active=True)
    await monitoring._watch_inplay_arb(session, sub, pusher=None, now=now)

    assert seen, "the scanner was never called — the watch returned before scanning"
    assert seen["min_lead_minutes"] < 0, (
        "min_lead_minutes must be negative or scan_arbs skips every started event, "
        f"which is every event this watch is for (got {seen['min_lead_minutes']})"
    )
    assert seen["max_age_minutes"] <= 5, (
        "in-play legs must be minutes old at most: the age window doubles as the maximum "
        f"gap between legs, and a wide one straddles kick-off (got {seen['max_age_minutes']})"
    )


async def test_the_inplay_watch_does_not_scan_when_nothing_is_live(session, monkeypatch) -> None:
    """Cheap and correct: no live events means no reason to touch the price history."""
    from sportsdata_agents.data.models import Subscription
    from sportsdata_agents.operations import monitoring

    now = dt.datetime.now(dt.UTC)
    _state(session, "finished", "ended", minutes_ago=1, now=now)
    await session.flush()

    called = False

    async def _fake_scan(_session, **kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr("sportsdata_agents.quant.arbitrage.scan_arbs", _fake_scan)
    sub = Subscription(name="t", kind="inplay_arb", params={}, active=True)
    assert await monitoring._watch_inplay_arb(session, sub, pusher=None, now=now) == 0
    assert not called


# ── capture: what gets polled, and what gets kept ─────────────────────

def _fixture(session, name: str, sport: str, *, starts_minutes_ago: float, now: dt.datetime):
    from sportsdata_agents.data.models import Fixture

    f = Fixture(sport=sport, external_id=name, name=name,
                start_time=now - dt.timedelta(minutes=starts_minutes_ago))
    session.add(f)
    return f


async def test_only_matches_that_could_be_running_are_polled(session) -> None:
    """The poll budget IS this query. Without it a capture pass costs whatever the
    catalogue happens to be; with it, it costs the number of matches actually on."""
    from sportsdata_agents.operations.ingestion.inplay import candidate_fixtures

    now = dt.datetime.now(dt.UTC)
    _fixture(session, "running_now", "afl", starts_minutes_ago=40, now=now)
    _fixture(session, "finished_last_week", "afl", starts_minutes_ago=60 * 24 * 7, now=now)
    _fixture(session, "starts_tomorrow", "afl", starts_minutes_ago=-60 * 24, now=now)
    await session.flush()

    names = {f.external_id for f in await candidate_fixtures(session, now=now)}
    assert names == {"running_now"}


async def test_the_window_follows_the_sport_not_one_global_guess(session) -> None:
    """A race is over in minutes and a tennis match can run five sets. One duration for
    both means either polling a finished race for hours, or going blind in a fifth set —
    and the fifth set is exactly when in-play prices matter."""
    from sportsdata_agents.operations.ingestion.inplay import candidate_fixtures

    now = dt.datetime.now(dt.UTC)
    _fixture(session, "race_long_over", "racing", starts_minutes_ago=90, now=now)
    _fixture(session, "tennis_fifth_set", "tennis", starts_minutes_ago=90, now=now)
    await session.flush()

    names = {f.external_id for f in await candidate_fixtures(session, now=now)}
    assert names == {"tennis_fifth_set"}


async def test_a_fixture_with_no_start_time_is_not_assumed_live(session) -> None:
    """Guessing wrong here means polling a book about a match that finished last week,
    every cycle, forever."""
    from sportsdata_agents.data.models import Fixture
    from sportsdata_agents.operations.ingestion.inplay import candidate_fixtures

    now = dt.datetime.now(dt.UTC)
    session.add(Fixture(sport="afl", external_id="unknown_start", name="unknown", start_time=None))
    await session.flush()

    assert await candidate_fixtures(session, now=now) == []


async def test_a_busy_saturday_is_truncated_rather_than_hammering_a_book(session) -> None:
    """More simultaneous matches than a sane request rate covers is the normal case, not
    the edge one. Seeing most live matches every cycle beats seeing all of them once and
    being rate-limited."""
    from sportsdata_agents.operations.ingestion.inplay import candidate_fixtures

    now = dt.datetime.now(dt.UTC)
    for i in range(60):
        _fixture(session, f"m{i}", "afl", starts_minutes_ago=30 + i * 0.1, now=now)
    await session.flush()

    assert len(await candidate_fixtures(session, now=now, max_events=40)) == 40


async def test_an_unchanged_score_writes_no_row(session) -> None:
    """Change-point shaped like `prices`: a row per poll would grow without bound while
    saying nothing."""
    from sportsdata_agents.operations.ingestion.inplay import LiveState, record_states

    now = dt.datetime.now(dt.UTC)
    obs = [LiveState("sportsbet", "afl", "e1", "live", 12, 7, "Q1 08:00")]

    first = await record_states(session, obs, captured_at=now)
    again = await record_states(session, obs, captured_at=now + dt.timedelta(seconds=30))
    assert (first, again) == (1, 0)

    moved = [LiveState("sportsbet", "afl", "e1", "live", 18, 7, "Q1 05:12")]
    assert await record_states(session, moved, captured_at=now + dt.timedelta(minutes=1)) == 1


async def test_a_status_change_is_recorded_even_when_the_score_has_not_moved(session) -> None:
    """Suspension is the case: the score is identical and the meaning is not. Missing it
    is how an in-play watch trusts a frozen leg."""
    from sportsdata_agents.operations.ingestion.inplay import LiveState, record_states

    now = dt.datetime.now(dt.UTC)
    await record_states(session, [LiveState("sportsbet", "afl", "e2", "live", 3, 3)], captured_at=now)
    written = await record_states(
        session, [LiveState("sportsbet", "afl", "e2", "suspended", 3, 3)],
        captured_at=now + dt.timedelta(seconds=20))
    assert written == 1


async def test_a_capture_pass_with_nothing_on_asks_the_provider_for_nothing(session) -> None:
    """No live matches means no provider call at all — the cheapest possible cycle, and
    the common one outside a match window."""
    from sportsdata_agents.operations.ingestion.inplay import capture_once

    called = False

    async def _fetch(fixtures):
        nonlocal called
        called = True
        return []

    report = await capture_once(session, _fetch, now=dt.datetime.now(dt.UTC))
    assert report == {"candidates": 0, "observed": 0, "recorded": 0}
    assert not called, "a provider must not be polled when nothing is running"
