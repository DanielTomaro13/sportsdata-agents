"""Exchange premium scan + watch: book price vs de-vigged Betfair fair."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import Event, Fixture, OddsSnapshot, Subscription
from sportsdata_agents.operations.monitoring import run_watches
from sportsdata_agents.quant.arbitrage import scan_exchange_premium

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 7, 6, 9, 0, tzinfo=dt.UTC)


def _snap(provider: str, book: str, event_id: str, selection: str, odds: float,
          event_name: str, market: str = "h2h") -> OddsSnapshot:
    return OddsSnapshot(
        captured_at=NOW - dt.timedelta(minutes=10), provider=provider, book=book,
        sport="tennis", event_external_id=event_id, event_name=event_name,
        market=market, selection=selection, odds=odds,
    )


async def _seed(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    fixture_id = uuid.uuid4()
    async with db_sessionmaker() as s:
        s.add(Fixture(id=fixture_id, sport="tennis", external_id="FX-1",
                      name="Alex De Minaur v Flavio Cobolli",
                      start_time=NOW + dt.timedelta(hours=3)))
        s.add(Event(fixture_id=fixture_id, provider="betfair", external_id="BF-1"))
        s.add(Event(fixture_id=fixture_id, provider="sportsbet", external_id="SB-1"))
        s.add(Event(fixture_id=fixture_id, provider="dabble", external_id="DB-1"))
        # Betfair back prices: 1.30 / 4.80 -> de-vig fair 0.7702 / 0.2086
        s.add(_snap("betfair", "Betfair", "BF-1", "home", 1.30,
                    "De Minaur v Cobolli"))
        s.add(_snap("betfair", "Betfair", "BF-1", "away", 4.80,
                    "De Minaur v Cobolli"))
        # Sportsbet pays 5.50 on Cobolli: 5.50 * 0.2086 - 1 = +14.7% premium
        s.add(_snap("sportsbet", "sportsbet", "SB-1", "home", 1.26,
                    "Alex De Minaur v Flavio Cobolli"))
        s.add(_snap("sportsbet", "sportsbet", "SB-1", "away", 5.50,
                    "Alex De Minaur v Flavio Cobolli"))
        # Dabble is inside fair on both sides — must NOT fire
        s.add(_snap("dabble", "Dabble", "DB-1", "home", 1.28,
                    "Alex De Minaur v Flavio Cobolli"))
        s.add(_snap("dabble", "Dabble", "DB-1", "away", 4.40,
                    "Alex De Minaur v Flavio Cobolli"))
        await s.commit()


async def test_scan_finds_the_premium_and_skips_fair_books(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_sessionmaker)
    async with db_sessionmaker() as s:
        found = await scan_exchange_premium(s, min_edge_pct=3.0, now=NOW)
    assert len(found) == 1, found
    hit = found[0]
    assert hit["book"] == "sportsbet" and hit["outcome"] == "away"
    assert hit["odds"] == 5.50
    # fair prob for away = (1/4.8) / (1/1.3 + 1/4.8); edge = 5.5 * fair - 1
    fair = (1 / 4.8) / (1 / 1.3 + 1 / 4.8)
    assert hit["edge_pct"] == pytest.approx(5.5 * fair * 100 - 100, abs=0.05)
    assert hit["exchange_fair_odds"] == pytest.approx(1 / fair, abs=0.01)


async def test_watch_fires_once_and_dedupes(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_sessionmaker)
    async with db_sessionmaker() as s:
        s.add(Subscription(tenant_id="t", workspace_id="w", name="exch",
                           kind="exchange_value", channel="log",
                           params={"min_edge_pct": 3.0, "hours": 2.0}))
        await s.commit()
    pushed: list[str] = []

    async def pusher(sub: Subscription, message: str) -> bool:
        pushed.append(message)
        return True

    report = await run_watches(db_sessionmaker, pusher=pusher, now=NOW)
    assert report["alerts"] == 1
    assert "exchange premium" in pushed[0] and "sportsbet" in pushed[0]
    # unchanged condition -> deduped
    report = await run_watches(db_sessionmaker, pusher=pusher, now=NOW + dt.timedelta(minutes=5))
    assert report["alerts"] == 0
