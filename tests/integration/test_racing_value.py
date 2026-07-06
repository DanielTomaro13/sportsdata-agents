"""Racing value: one book out vs Betfair (or the pack), alerts with horse names."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import OddsSnapshot, Subscription
from sportsdata_agents.operations.monitoring import run_watches
from sportsdata_agents.quant.racing_value import scan_racing_value

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 7, 6, 4, 0, tzinfo=dt.UTC)
JUMP = NOW + dt.timedelta(minutes=25)


def _book_row(book: str, event_id: str, number: int, runner: str, odds: float) -> OddsSnapshot:
    return OddsSnapshot(
        captured_at=NOW - dt.timedelta(minutes=5), provider=book.lower(), book=book,
        sport="horse_racing", event_external_id=event_id, event_name="Pakenham R5",
        market="win", selection=str(number), odds=odds, start_time=JUMP,
        meta={"runner": runner},
    )


def _betfair_row(runner: str, number: int, odds: float) -> OddsSnapshot:
    return OddsSnapshot(
        captured_at=NOW - dt.timedelta(minutes=4), provider="betfair", book="Betfair",
        sport="horse_racing", event_external_id="BF-MEETING",
        event_name="Pakenham (AUS) 6th Jul", market="win",
        selection=runner.lower(), odds=odds, start_time=JUMP,
        meta={"runner": runner, "runner_number": number,
              "race": "R5 1400m Hcap", "market_id": "1.999"},
    )


async def _seed(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with db_sessionmaker() as s:
        # Betfair fair (de-vig of 2.2/3.4/4.8/9.0): the truth
        for runner, number, odds in (("Boat Race", 1, 2.2), ("Silver Comet", 2, 3.4),
                                     ("Rusty Rancher", 4, 4.8), ("Night Parade", 7, 9.0)):
            s.add(_betfair_row(runner, number, odds))
        # PointsBet is OUT on Rusty Rancher: fair ~5.36, they pay 8.00 (+49%)
        for book, event_id, prices in (
            ("PointsBet", "PB-R5", ((1, "Boat Race", 2.10), (2, "Silver Comet", 3.20),
                                    (4, "Rusty Rancher", 8.00), (7, "Night Parade", 8.00))),
            ("TAB", "TAB-R5", ((1, "Boat Race", 2.15), (2, "Silver Comet", 3.30),
                               (4, "Rusty Rancher", 4.60), (7, "Night Parade", 8.50))),
            ("Sportsbet", "SB-R5", ((1, "Boat Race", 2.10), (2, "Silver Comet", 3.25),
                                    (4, "Rusty Rancher", 4.50), (7, "Night Parade", 8.20))),
            ("Ladbrokes", "LB-R5", ((1, "Boat Race", 2.12), (2, "Silver Comet", 3.28),
                                    (4, "Rusty Rancher", 4.55), (7, "Night Parade", 8.30))),
        ):
            for number, runner, odds in prices:
                s.add(_book_row(book, event_id, number, runner, odds))
        await s.commit()


async def test_scan_flags_the_out_book_with_horse_details(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_sessionmaker)
    async with db_sessionmaker() as s:
        found = await scan_racing_value(s, min_edge_pct=8.0, now=NOW)
    assert len(found) == 1, found
    hit = found[0]
    assert hit["book"] == "PointsBet" and hit["runner"] == "Rusty Rancher"
    assert hit["runner_number"] == 4 and hit["race"] == "Pakenham R5"
    assert hit["versus"] == "Betfair"
    inv = 1 / 2.2 + 1 / 3.4 + 1 / 4.8 + 1 / 9.0
    fair = (1 / 4.8) / inv
    assert hit["edge_pct"] == pytest.approx(8.0 * fair * 100 - 100, abs=0.05)


async def test_consensus_mode_without_the_exchange(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_sessionmaker)
    async with db_sessionmaker() as s:
        # ignore Betfair entirely: the pack (TAB/Sportsbet medians) still flags it
        found = await scan_racing_value(s, exchange_book="NoSuchExchange",
                                        min_edge_pct=8.0, now=NOW)
    hit = next(c for c in found if c["book"] == "PointsBet" and c["runner"] == "Rusty Rancher")
    assert "consensus" in hit["versus"]


async def test_watch_message_carries_names_not_ids(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_sessionmaker)
    async with db_sessionmaker() as s:
        s.add(Subscription(tenant_id="t", workspace_id="w", name="racing",
                           kind="racing_value", channel="log",
                           params={"min_edge_pct": 8.0}))
        await s.commit()
    pushed: list[str] = []

    async def pusher(sub: Subscription, message: str) -> bool:
        pushed.append(message)
        return True

    report = await run_watches(db_sessionmaker, pusher=pusher, now=NOW)
    assert report["alerts"] == 1
    message = pushed[0]
    assert "Pakenham R5" in message and "Rusty Rancher" in message and "(#4)" in message
    assert "PB-R5" not in message  # ids never reach the phone
    # unchanged race -> deduped
    report = await run_watches(db_sessionmaker, pusher=pusher, now=NOW + dt.timedelta(minutes=5))
    assert report["alerts"] == 0
