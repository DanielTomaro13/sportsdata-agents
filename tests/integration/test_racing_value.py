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


def _betfair_row(runner: str, number: int, odds: float,
                 matched: float = 25_000.0) -> OddsSnapshot:
    # matched rides every exchange row: the scan refuses a fair from a
    # near-untraded race, so seeds model a LIQUID market by default
    return OddsSnapshot(
        captured_at=NOW - dt.timedelta(minutes=4), provider="betfair", book="Betfair",
        sport="horse_racing", event_external_id="BF-MEETING",
        event_name="Pakenham (AUS) 6th Jul", market="win",
        selection=runner.lower(), odds=odds, start_time=JUMP,
        meta={"runner": runner, "runner_number": number, "total_matched": matched,
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
    assert hit["exchange_matched"] == 25_000.0  # traded volume rides the alert
    inv = 1 / 2.2 + 1 / 3.4 + 1 / 4.8 + 1 / 9.0
    fair = (1 / 4.8) / inv
    assert hit["edge_pct"] == pytest.approx(8.0 * fair * 100 - 100, abs=0.05)


async def test_thin_betfair_race_falls_back_to_consensus(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A near-untraded exchange race must not be the fair source — the scan
    demotes it and prices off the pack instead."""
    async with db_sessionmaker() as s:
        for runner, number, odds in (("Boat Race", 1, 2.2), ("Silver Comet", 2, 3.4),
                                     ("Rusty Rancher", 4, 4.8), ("Night Parade", 7, 9.0)):
            s.add(_betfair_row(runner, number, odds, matched=40.0))  # $40 traded
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
    async with db_sessionmaker() as s:
        found = await scan_racing_value(s, min_edge_pct=8.0, now=NOW)
    assert found, "the consensus path must still flag the out book"
    assert all(c["versus"].startswith("consensus") for c in found)
    assert all(c["exchange_matched"] is None for c in found)


async def test_absurd_edge_is_refused_as_a_data_artifact(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A runner whose fair is trustworthy (inside max_fair_odds) but whose book
    price implies an enormous edge is a mis-read/scratched-runner artifact, not
    a bet — the max_edge_pct ceiling refuses it."""
    async with db_sessionmaker() as s:
        for runner, number, odds in (("Boat Race", 1, 2.2), ("Silver Comet", 2, 3.4),
                                     ("Rusty Rancher", 4, 4.8), ("Night Parade", 7, 9.0)):
            s.add(_betfair_row(runner, number, odds))  # fair on Rusty ~5.36
        # PointsBet lists Rusty at 15.00 — a +180% "edge" no clean market offers
        for book, event_id, prices in (
            ("PointsBet", "PB-R5", ((1, "Boat Race", 2.10), (2, "Silver Comet", 3.20),
                                    (4, "Rusty Rancher", 15.00), (7, "Night Parade", 8.00))),
            ("TAB", "TAB-R5", ((1, "Boat Race", 2.15), (2, "Silver Comet", 3.30),
                               (4, "Rusty Rancher", 4.60), (7, "Night Parade", 8.50))),
            ("Sportsbet", "SB-R5", ((1, "Boat Race", 2.10), (2, "Silver Comet", 3.25),
                                    (4, "Rusty Rancher", 4.50), (7, "Night Parade", 8.20))),
        ):
            for number, runner, odds in prices:
                s.add(_book_row(book, event_id, number, runner, odds))
        await s.commit()
    async with db_sessionmaker() as s:
        found = await scan_racing_value(s, min_edge_pct=8.0, now=NOW)
    assert not any(c["runner"] == "Rusty Rancher" and c["edge_pct"] > 60.0 for c in found), found


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


async def test_watch_requires_betfair_or_engine_shorter(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The operator's racing rule: a consensus-only edge (thin Betfair, no
    engine fair under the book's price) stays SILENT by default — the signal
    is Betfair or the engine shorter than the bookmaker, not the pack median.
    require_sharp_fair=false restores the consensus alerts."""
    async with db_sessionmaker() as s:
        # Betfair nearly untraded: demoted, so the scan falls back to consensus
        for runner, number, odds in (("Boat Race", 1, 2.2), ("Silver Comet", 2, 3.4),
                                     ("Rusty Rancher", 4, 4.8), ("Night Parade", 7, 9.0)):
            s.add(_betfair_row(runner, number, odds, matched=40.0))
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
        s.add(Subscription(tenant_id="t", workspace_id="w", name="racing",
                           kind="racing_value", channel="log",
                           params={"min_edge_pct": 8.0}))
        await s.commit()
    pushed: list[str] = []

    async def pusher(sub: Subscription, message: str) -> bool:
        pushed.append(message)
        return True

    report = await run_watches(db_sessionmaker, pusher=pusher, now=NOW)
    assert report["alerts"] == 0 and pushed == []

    # the operator opts back into consensus-only alerts explicitly
    async with db_sessionmaker() as s:
        sub = (await s.execute(Subscription.__table__.select())).first()
        await s.execute(Subscription.__table__.update()
                        .where(Subscription.id == sub.id)
                        .values(params={"min_edge_pct": 8.0, "require_sharp_fair": False}))
        await s.commit()
    report = await run_watches(db_sessionmaker, pusher=pusher, now=NOW)
    assert report["alerts"] == 1 and "Rusty Rancher" in pushed[0]


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


async def test_board_drops_na_books_and_verifies_runner_names(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The cross-book board lists PRICED books only (NA books fold into a
    count) and a row that names a different runner never joins, even when its
    saddle number matches (lived: a novelty product's 'Evens' at 2.60 rode a
    21.00 horse's board as runner 2)."""
    from sportsdata_agents.operations.monitoring import _format_board, _racing_board

    live_now = dt.datetime.now(dt.UTC)  # the board window is wall-clock, not NOW

    def _fresh_row(book: str, runner: str, odds: float) -> OddsSnapshot:
        return OddsSnapshot(
            captured_at=live_now - dt.timedelta(minutes=2), provider=book.lower(),
            book=book, sport="horse_racing", event_external_id=f"{book}-R5",
            event_name="Pakenham R5", market="win", selection="2", odds=odds,
            start_time=live_now + dt.timedelta(minutes=25), meta={"runner": runner})

    async with db_sessionmaker() as s:
        for book, runner, odds in (("PointsBet", "Last Clansman", 23.0),
                                   ("TAB", "Last Clansman", 21.0)):
            s.add(_fresh_row(book, runner, odds))
        # Ladbrokes lists selection "2" too — but names a DIFFERENT runner
        s.add(_fresh_row("Ladbrokes", "Evens", 2.60))
        await s.commit()
    async with db_sessionmaker() as s:
        quotes, thin = await _racing_board(s, "Pakenham R5", "win", "2", "PointsBet")
    assert quotes.get("TAB") == 21.0
    assert "Ladbrokes" not in quotes  # runner name disagreed — never joined
    board = _format_board(quotes, ["Pinnacle", "Betfair"],
                          ("Sportsbet", "TAB", "Dabble"), thin=thin, engine_fair=20.0)
    assert "NA" not in board.replace("books NA", "")  # no NA cells
    assert "3 books NA" in board or "4 books NA" in board  # folded into a count
    assert "**TAB 21.00**" in board  # the industry's best price is bolded
