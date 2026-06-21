"""Resolver: an exchange's END time (resolution/expiry) is a day-window PROXY only — it
must never become the fixture's start_time, or the arb in-play gate would read a live game
as still pre-game. A book's real start wins (founded or backfilled)."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sportsdata_agents.data.base import Base
from sportsdata_agents.data.models import Event, Fixture
from sportsdata_agents.operations.ingestion import PricePoint, record_points
from sportsdata_agents.operations.resolution.resolver import resolve_events

pytestmark = pytest.mark.unit


async def _sf() -> async_sessionmaker:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).isoformat()


def _aware(t: dt.datetime) -> dt.datetime:
    # SQLite returns tz-naive datetimes (stored as UTC) — re-attach UTC for comparison.
    return t if t.tzinfo else t.replace(tzinfo=dt.UTC)


async def test_book_real_start_wins_over_exchange_end_proxy() -> None:
    sf = await _sf()
    now = dt.datetime.now(dt.UTC)
    start = now + dt.timedelta(hours=2)  # real kickoff
    end = now + dt.timedelta(hours=4)  # exchange resolution ≈ game end
    cap = now - dt.timedelta(minutes=1)

    # Record the EXCHANGE first (founds the fixture with NO real start), then the book
    # (whose real start must backfill). Same match name → resolver joins them.
    await record_points(sf, [PricePoint(
        provider="kalshi", book="Kalshi", sport="basketball", event_external_id="K1",
        event_name="Thunder v Pacers", market="h2h", selection="Thunder", odds=1.9,
        meta={"end_time": _iso(end)})], captured_at=cap)
    await record_points(sf, [PricePoint(
        provider="sportsbet", book="Sportsbet", sport="basketball", event_external_id="S1",
        event_name="Thunder v Pacers", market="h2h", selection="Thunder", odds=1.95,
        meta={"start_time": _iso(start)})], captured_at=cap)

    await resolve_events(sf)

    async with sf() as s:
        fixtures = (await s.execute(select(Fixture))).scalars().all()
        events = (await s.execute(select(Event))).scalars().all()
        assert len(fixtures) == 1, "book + exchange should resolve to ONE fixture"
        fx = fixtures[0]
        # the REAL start (from the book) wins — not the exchange end-proxy
        assert fx.start_time is not None
        assert abs((_aware(fx.start_time) - start).total_seconds()) < 2
        assert {e.fixture_id for e in events} == {fx.id}  # both events mapped to it


async def test_exchange_only_fixture_has_no_real_start() -> None:
    sf = await _sf()
    now = dt.datetime.now(dt.UTC)
    end = now + dt.timedelta(hours=4)
    cap = now - dt.timedelta(minutes=1)

    await record_points(sf, [PricePoint(
        provider="polymarket", book="Polymarket", sport="basketball", event_external_id="P1",
        event_name="Lakers v Celtics", market="h2h", selection="Lakers", odds=2.0,
        meta={"end_time": _iso(end)})], captured_at=cap)

    await resolve_events(sf)

    async with sf() as s:
        fx = (await s.execute(select(Fixture))).scalars().one()
        # No book joined → no real start → the arb in-play gate will (correctly) skip it,
        # but the end-proxy still let it resolve/bucket.
        assert fx.start_time is None
        assert fx.end_time is not None
        assert abs((_aware(fx.end_time) - end).total_seconds()) < 2
