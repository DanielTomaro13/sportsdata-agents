"""Sports board reader: assembles cross-source h2h from the warehouse, blends
the sharp line (only the sharps), values the books, and lists games."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import (
    Event,
    Fixture,
    ModelArtifact,
    OddsSnapshot,
    Prediction,
)
from sportsdata_agents.interfaces.sportsboard.warehouse import game_detail, list_games

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 7, 21, 12, 0, tzinfo=dt.UTC)


async def _seed_game(s: AsyncSession, *, fixture_id, quotes: dict[str, dict[str, float]],
                     start, betfair_meta: dict | None = None) -> None:
    for book, sides in quotes.items():
        ext = f"{book}-evt"
        s.add(Event(provider=book.lower(), external_id=ext, fixture_id=fixture_id))
        for side, odds in sides.items():
            s.add(OddsSnapshot(
                provider=book.lower(), book=book, sport="basketball",
                event_external_id=ext, event_name="Lakers v Celtics",
                market="h2h", selection=side, odds=odds,
                captured_at=NOW - dt.timedelta(minutes=2), start_time=start,
                meta=(betfair_meta if book == "Betfair" else {})))


async def test_detail_blends_only_sharps_and_values_books(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    fix_id = None
    async with db_sessionmaker() as s:
        f = Fixture(sport="basketball", external_id="g1", name="Lakers v Celtics",
                    start_time=NOW + dt.timedelta(hours=2))
        s.add(f)
        await s.flush()
        fix_id = f.id
        await _seed_game(s, fixture_id=f.id, start=f.start_time, quotes={
            "Kalshi": {"home": 1.95, "away": 1.95},
            "Polymarket": {"home": 2.0, "away": 2.0},
            "Betfair": {"home": 2.02, "away": 1.98},
            "Pinnacle": {"home": 2.0, "away": 2.0},
            "Sportsbet": {"home": 2.20, "away": 1.75},   # book: home is value
            "TAB": {"home": 2.05, "away": 1.80},
        }, betfair_meta={"total_matched": 84000.0, "back_size": 900, "lay": 300})
        await s.commit()

    async with db_sessionmaker() as s:
        d = await game_detail(s, str(fix_id), now=NOW)

    assert d is not None
    assert set(d["sharp_sources"]) == {"Kalshi", "Polymarket", "Betfair", "Pinnacle"}
    assert abs(d["fair"]["home"] - 0.5) < 0.02        # all sharps near evens
    # Sportsbet 2.20 home is the best book price and shows value
    assert d["value"]["home"]["best_book"] == "Sportsbet"
    assert d["value"]["home"]["value_pct"] > 0
    # sharps are excluded from "best book"
    assert d["value"]["home"]["best_book"] not in ("Kalshi", "Polymarket", "Betfair", "Pinnacle")
    assert d["bf_money"]["matched"] == 84000.0
    assert d["home"] == "Lakers" and d["away"] == "Celtics"


async def test_afl_game_falls_back_to_the_sharps_present(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        f = Fixture(sport="afl", external_id="a1", name="Lions v Bombers",
                    start_time=NOW + dt.timedelta(hours=3))
        s.add(f)
        await s.flush()
        fid = f.id
        # AFL: no Kalshi/Polymarket, only Betfair + Pinnacle + books
        for book, sides in {"Betfair": {"home": 1.8, "away": 2.1},
                            "Pinnacle": {"home": 1.83, "away": 2.05},
                            "Sportsbet": {"home": 1.75, "away": 2.20}}.items():
            ext = f"{book}-a"
            s.add(Event(provider=book.lower(), external_id=ext, fixture_id=f.id))
            for side, odds in sides.items():
                s.add(OddsSnapshot(provider=book.lower(), book=book, sport="afl",
                                   event_external_id=ext, event_name="Lions v Bombers",
                                   market="h2h", selection=side, odds=odds,
                                   captured_at=NOW - dt.timedelta(minutes=1),
                                   start_time=f.start_time, meta={}))
        await s.commit()
        d = await game_detail(s, str(fid), now=NOW)
    assert set(d["sharp_sources"]) == {"Betfair", "Pinnacle"}
    assert d["value"]["away"]["best_book"] == "Sportsbet"  # 2.20 the longest away


async def test_list_games_summarises_coverage_and_favourite(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        f = Fixture(sport="basketball", external_id="g2", name="Suns v Heat",
                    start_time=NOW + dt.timedelta(hours=1))
        s.add(f)
        await s.flush()
        await _seed_game(s, fixture_id=f.id, start=f.start_time, quotes={
            "Betfair": {"home": 1.5, "away": 2.8},   # home strong fav
            "Pinnacle": {"home": 1.52, "away": 2.7},
            "Sportsbet": {"home": 1.48, "away": 2.9},
        })
        # a stale game outside the window must not appear
        old = Fixture(sport="nfl", external_id="g3", name="A v B",
                      start_time=NOW - dt.timedelta(hours=5))
        s.add(old)
        await s.commit()

        games = await list_games(s, hours=12.0, now=NOW)
    assert len(games) == 1
    g = games[0]
    assert g["name"] == "Suns v Heat"
    assert g["favourite"] == "home" and g["fav_prob"] > 0.6
    assert set(g["sharp_sources"]) == {"Betfair", "Pinnacle"} and g["book_count"] == 1


async def test_engine_rating_joins_by_fixture_id(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        f = Fixture(sport="basketball", external_id="g4", name="Bulls v Knicks",
                    start_time=NOW + dt.timedelta(hours=2))
        s.add(f)
        await s.flush()
        await _seed_game(s, fixture_id=f.id, start=f.start_time, quotes={
            "Betfair": {"home": 2.0, "away": 2.0}, "Pinnacle": {"home": 2.0, "away": 2.0}})
        m = ModelArtifact(tenant_id="t", workspace_id="w",
                          name="engine-ratings:basketball", version=1,
                          sport="basketball", params={}, calibration={})
        s.add(m)
        await s.flush()
        for side, p in (("home", 0.58), ("away", 0.42)):
            s.add(Prediction(tenant_id="t", workspace_id="w", model_id=m.id,
                             provider="engine", event_external_id=str(f.id),
                             market="h2h", selection=side, prob=p,
                             predicted_at=NOW))
        await s.commit()
        d = await game_detail(s, str(f.id), now=NOW)
    assert d["engine_rating"] == {"home": 0.58, "away": 0.42}


async def test_detail_returns_totals_and_spreads_not_just_h2h(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """As many markets as possible: h2h + total + line each get a sharp line."""
    async with db_sessionmaker() as s:
        f = Fixture(sport="nfl", external_id="mm", name="Chiefs v Bills",
                    start_time=NOW + dt.timedelta(hours=2))
        s.add(f)
        await s.flush()
        # three markets across sources: h2h, total 47.5 (o/u), line -3.5 (home/away)
        rows = {
            "Betfair": {"h2h": {"home": 1.9, "away": 2.0},
                        "total 47.5": {"over": 1.92, "under": 1.92},
                        "line -3.5": {"home": 1.9, "away": 1.95}},
            "Pinnacle": {"h2h": {"home": 1.92, "away": 1.98},
                         "total 47.5": {"over": 1.9, "under": 1.93},
                         "line -3.5": {"home": 1.91, "away": 1.94}},
            "Sportsbet": {"h2h": {"home": 1.85, "away": 2.10},
                          "total 47.5": {"over": 1.95, "under": 1.85},
                          "line -3.5": {"home": 1.88, "away": 2.0}},
        }
        for book, mkts in rows.items():
            ext = f"{book}-mm"
            s.add(Event(provider=book.lower(), external_id=ext, fixture_id=f.id))
            for market, sides in mkts.items():
                for side, odds in sides.items():
                    sel = side if market == "h2h" else f"{side} {market.split()[1]}"
                    s.add(OddsSnapshot(provider=book.lower(), book=book, sport="nfl",
                                       event_external_id=ext, event_name="Chiefs v Bills",
                                       market=market.split()[0] if " " in market else market,
                                       selection=sel, odds=odds,
                                       captured_at=NOW - dt.timedelta(minutes=1),
                                       start_time=f.start_time, meta={}))
        await s.commit()
        d = await game_detail(s, str(f.id), now=NOW)

    families = {m["family"] for m in d["markets"]}
    assert families == {"h2h", "total", "line"}
    assert d["markets"][0]["family"] == "h2h"          # h2h leads
    total = next(m for m in d["markets"] if m["family"] == "total")
    assert set(total["fair"]) == {"over", "under"}
    assert total["value"]["over"]["best_book"] == "Sportsbet"   # 1.95 the longest over
    line = next(m for m in d["markets"] if m["family"] == "line")
    assert line["line"] == -3.5


async def test_market_flow_tracks_the_sharp_line_and_money_over_time(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Money flow: reconstruct the blended sharp line + Betfair matched at
    time buckets across the window, and the open->now drift."""
    from sportsdata_agents.interfaces.sportsboard.warehouse import (
        _fixture_events,
        market_flow,
    )
    async with db_sessionmaker() as s:
        f = Fixture(sport="basketball", external_id="fl", name="Nuggets v Suns",
                    start_time=NOW + dt.timedelta(hours=1))
        s.add(f)
        await s.flush()
        s.add(Event(provider="betfair", external_id="bf-fl", fixture_id=f.id))
        # home steams from 2.20 (0.45) to 1.80 (0.56) over 6 snapshots; matched grows
        for i in range(6):
            cap = NOW - dt.timedelta(hours=5) + dt.timedelta(hours=i)
            home = 2.20 - i * 0.08
            away = 1.80 + i * 0.10
            for side, o in (("home", home), ("away", away)):
                s.add(OddsSnapshot(provider="betfair", book="Betfair", sport="basketball",
                                   event_external_id="bf-fl", event_name="Nuggets v Suns",
                                   market="h2h", selection=side, odds=o, captured_at=cap,
                                   start_time=f.start_time,
                                   meta={"total_matched": 10000.0 * (i + 1)} if side == "home" else {}))
        await s.commit()
        events = (await _fixture_events(s, {f.id}))[f.id]
        flow = await market_flow(s, events, now=NOW, window_hours=8.0)

    assert flow["sharp_series"]                       # a reconstructed series
    assert flow["moves"]["home"]["delta"] > 0         # home firmed (prob rose)
    assert flow["moves"]["away"]["delta"] < 0         # away drifted
    assert flow["matched_now"] == 60000.0             # latest matched
    assert flow["matched_delta_60m"] is not None
