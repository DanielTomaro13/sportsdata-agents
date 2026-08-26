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


# ── the id-dialect contract, and the shipped fetcher ─────────────────

async def test_live_event_ids_answers_in_the_fixture_uuid_dialect(session) -> None:
    """THE SECOND WAY THE WATCH WAS DEAD. `scan_arbs` results carry only the warehouse
    fixture UUID; match_state rows carried only the provider's event id. The watch
    compared one against a set of the other, so even with capture running it could
    never match. The set now answers in both dialects."""
    import uuid as _uuid

    from sportsdata_agents.data.models import MatchState
    from sportsdata_agents.operations.monitoring import live_event_ids

    fid = _uuid.uuid4()
    now = dt.datetime.now(dt.UTC)
    session.add(MatchState(captured_at=now - dt.timedelta(minutes=1), provider="sportsbet",
                           sport="afl", event_external_id="6543210", fixture_id=fid, status="live"))
    await session.flush()

    live = await live_event_ids(session, now=now)
    assert "6543210" in live, "the provider's own dialect must still work"
    assert str(fid) in live, "the fixture-UUID dialect is what scan_arbs speaks"


async def test_the_watch_matches_an_arb_by_fixture_uuid_end_to_end(session, monkeypatch) -> None:
    """The full contract: a captured live state and a scanned arb meet on the fixture
    UUID and the alert fires. Tests the join itself, which is where both previous
    dead-watch bugs lived."""
    import uuid as _uuid

    from sportsdata_agents.data.models import MatchState, Subscription
    from sportsdata_agents.operations import monitoring

    fid = _uuid.uuid4()
    now = dt.datetime.now(dt.UTC)
    session.add(MatchState(captured_at=now - dt.timedelta(minutes=1), provider="sportsbet",
                           sport="afl", event_external_id="777", fixture_id=fid, status="live"))
    await session.flush()

    arb = {
        "fixture_id": fid, "fixture": "Bulldogs v Tigers", "sport": "afl",
        "market": "h2h", "line": "", "margin_pct": 2.1, "sum_inverse": 0.979,
        "legs": [
            {"outcome": "Bulldogs", "book": "Sportsbet", "odds": 2.10, "stake_share": 0.49},
            {"outcome": "Tigers", "book": "TAB", "odds": 2.05, "stake_share": 0.51},
        ],
    }

    async def _fake_scan(_session, **kwargs):
        return [arb]

    fired: list[dict] = []

    async def _fake_fire(_session, _sub, *, kind, key, message, payload, pusher):
        fired.append({"kind": kind, "key": key})
        return True

    monkeypatch.setattr("sportsdata_agents.quant.arbitrage.scan_arbs", _fake_scan)
    monkeypatch.setattr(monitoring, "_fire", _fake_fire)
    sub = Subscription(name="t", kind="inplay_arb", params={}, active=True)
    count = await monitoring._watch_inplay_arb(session, sub, pusher=None, now=now)
    assert count == 1 and fired[0]["kind"] == "inplay_arb"


def test_sportsbet_status_precedence_ended_beats_suspended_beats_live() -> None:
    """A settled event can carry a lingering suspended flag, and a suspended one is
    usually still inPlay. Each earlier state makes the later flags meaningless — and
    mapping suspended to live is specifically how a watch trusts a frozen leg."""
    from sportsdata_agents.operations.ingestion.inplay import _status_from_sportsbet

    assert _status_from_sportsbet({"status": "SETTLED", "inPlay": True, "suspended": True}) == "ended"
    assert _status_from_sportsbet({"status": "", "inPlay": True, "suspended": True}) == "suspended"
    assert _status_from_sportsbet({"status": "", "inPlay": True, "suspended": False}) == "live"
    assert _status_from_sportsbet({"status": "", "inPlay": False, "suspended": False}) == "pre"
    assert _status_from_sportsbet(["not", "a", "dict"]) is None
    assert _status_from_sportsbet(None) is None


class _StatusManager:
    """The data plane, reduced to one status endpoint. Records what was asked."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, args: dict) -> dict:
        self.calls.append((name, args))
        return dict(self.payload)


async def _mapped_live_fixture(sessionmaker, *, now, event_id="12345", provider="sportsbet"):
    from sportsdata_agents.data.models import Event, Fixture

    async with sessionmaker() as s:
        fixture = Fixture(sport="afl", external_id="afl-1", name="Bulldogs v Tigers",
                          start_time=now - dt.timedelta(minutes=40))
        s.add(fixture)
        await s.flush()
        s.add(Event(provider=provider, external_id=event_id, fixture_id=fixture.id))
        fid = fixture.id
        await s.commit()
    return fid


async def test_inplay_pass_captures_a_mapped_live_match(db_sessionmaker) -> None:
    """End to end: candidate fixture → its sportsbet event mapping → one status call →
    a match_state row the arb watch can actually join on."""
    from sportsdata_agents.operations.ingestion.inplay import inplay_pass
    from sportsdata_agents.operations.monitoring import live_event_ids

    now = dt.datetime.now(dt.UTC)
    fid = await _mapped_live_fixture(db_sessionmaker, now=now)
    manager = _StatusManager({"eventId": 12345, "status": "", "inPlay": True, "suspended": False})

    report = await inplay_pass(manager, db_sessionmaker, now=now)
    assert report == {"ok": True, "candidates": 1, "observed": 1, "recorded": 1}
    assert manager.calls == [("sportsbet_event_status", {"eventId": 12345})], (
        "eventId must be the INTEGER sportsbet id, exactly once per mapped event"
    )
    async with db_sessionmaker() as s:
        live = await live_event_ids(s, now=now)
    assert str(fid) in live, "the captured state must be joinable by fixture UUID"


async def test_inplay_pass_respects_the_off_switch(db_sessionmaker, monkeypatch) -> None:
    """SPORTSDATA_AGENTS_INPLAY=0 stops the pass before any provider traffic — the
    operator's cold stop, checked first so it costs nothing to be off."""
    from sportsdata_agents.operations.ingestion.inplay import inplay_pass

    now = dt.datetime.now(dt.UTC)
    await _mapped_live_fixture(db_sessionmaker, now=now, event_id="999")
    manager = _StatusManager({"inPlay": True})
    monkeypatch.setenv("SPORTSDATA_AGENTS_INPLAY", "0")

    assert await inplay_pass(manager, db_sessionmaker, now=now) == {"ok": True, "disabled": True}
    assert manager.calls == []


async def test_inplay_pass_only_calls_about_this_books_own_events(db_sessionmaker) -> None:
    """An espn mapping is not a sportsbet event id, and a non-integer external id is not
    one either. Calling the status endpoint with a foreign id is a wasted request
    against a budget measured in bans."""
    from sportsdata_agents.operations.ingestion.inplay import inplay_pass

    now = dt.datetime.now(dt.UTC)
    await _mapped_live_fixture(db_sessionmaker, now=now, event_id="401234", provider="espn")
    manager = _StatusManager({"inPlay": True})

    report = await inplay_pass(manager, db_sessionmaker, now=now)
    assert report["candidates"] == 1 and report["observed"] == 0
    assert manager.calls == []


async def test_ingest_once_dispatches_a_run_feed_around_the_price_path(db_sessionmaker) -> None:
    """Feed.run is the escape hatch for feeds that are not price-shaped; the price
    pipeline (fetch → normalize → record_points) must never execute for one."""
    from sportsdata_agents.operations.ingestion.worker import Feed, ingest_once

    def _must_not_normalize(_payload):
        raise AssertionError("the price path ran for a run-feed")

    async def _pass(manager, session_factory):
        return {"ok": True, "marker": "ran"}

    feed = Feed(name="state_probe", tool="unused", mcp_groups=(), provider="probe",
                normalizer=_must_not_normalize, run=_pass)
    report = await ingest_once(_StatusManager({}), db_sessionmaker, [feed])
    assert report == {"state_probe": {"ok": True, "marker": "ran"}}
