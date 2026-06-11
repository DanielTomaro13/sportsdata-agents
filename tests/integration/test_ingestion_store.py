"""M2.1 exit gates: capture over time → dedupe to change-points → line-movement query;
the worker isolates feed failures and honours per-feed schedules."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import OddsSnapshot, Price
from sportsdata_agents.operations.ingestion import (
    Feed,
    PricePoint,
    ingest_once,
    line_movement,
    prune_snapshots,
    record_points,
    run_loop,
)

pytestmark = pytest.mark.integration

T0 = dt.datetime(2026, 6, 10, 9, 0, tzinfo=dt.UTC)
T1 = T0 + dt.timedelta(minutes=5)
T2 = T0 + dt.timedelta(minutes=10)


def _point(odds: float, selection: str = "home", book: str = "TabAustralia") -> PricePoint:
    return PricePoint(
        provider="nba_cdn",
        book=book,
        sport="nba",
        event_external_id="0042500403",
        market="2way",
        selection=selection,
        odds=odds,
    )


async def test_capture_dedupe_and_line_movement(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """The M2.1 exit gate: three captures, one price move → snapshots keep every
    observation, prices keep only the change-points, the movement query shows them."""
    await record_points(db_sessionmaker, [_point(1.85), _point(2.00, "away")], captured_at=T0)
    await record_points(db_sessionmaker, [_point(1.85), _point(2.00, "away")], captured_at=T1)  # unchanged
    await record_points(db_sessionmaker, [_point(1.92), _point(2.00, "away")], captured_at=T2)  # home moves

    async with db_sessionmaker() as s:
        assert (await s.execute(select(func.count()).select_from(OddsSnapshot))).scalar_one() == 6
        assert (await s.execute(select(func.count()).select_from(Price))).scalar_one() == 3  # 2 first + 1 move

    movement = await line_movement(db_sessionmaker, event_external_id="0042500403", selection="home")
    assert [(m["prev_odds"], m["odds"]) for m in movement] == [(None, 1.85), (1.85, 1.92)]
    assert movement[0]["changed_at"] < movement[1]["changed_at"]

    away = await line_movement(db_sessionmaker, event_external_id="0042500403", selection="away")
    assert len(away) == 1  # never moved → one first-sighting row


async def test_prune_keeps_recent_and_all_change_points(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    old = dt.datetime.now(dt.UTC) - dt.timedelta(days=120)
    await record_points(db_sessionmaker, [_point(1.85)], captured_at=old)
    await record_points(db_sessionmaker, [_point(1.92)])  # now
    assert await prune_snapshots(db_sessionmaker, older_than_days=90) == 1
    async with db_sessionmaker() as s:
        assert (await s.execute(select(func.count()).select_from(OddsSnapshot))).scalar_one() == 1
        assert (await s.execute(select(func.count()).select_from(Price))).scalar_one() == 2  # series intact


class FakeManager:
    """Quacks like MCPManager.call_tool; scriptable per tool."""

    def __init__(self, payloads: dict[str, Any]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        self.calls.append(name)
        out = self.payloads[name]
        if isinstance(out, Exception):
            raise out
        return out


NBA_MINI = {
    "games": [
        {
            "gameId": "G1",
            "markets": [
                {"name": "2way", "books": [{"name": "B", "outcomes": [{"type": "home", "odds": "1.90"}]}]}
            ],
        }
    ]
}


async def test_ingest_once_isolates_feed_failures(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    from sportsdata_agents.operations.ingestion.normalizers import normalize_nba_odds

    good = Feed(name="good", tool="nba_odds_today", mcp_groups=("nba",), normalizer=normalize_nba_odds)
    bad = Feed(name="bad", tool="broken_feed", mcp_groups=("nba",), normalizer=normalize_nba_odds)
    manager = FakeManager({"nba_odds_today": NBA_MINI, "broken_feed": RuntimeError("upstream 500")})

    report = await ingest_once(manager, db_sessionmaker, [bad, good])
    assert report["bad"]["ok"] is False and "upstream 500" in report["bad"]["error"]
    assert report["good"] == {"ok": True, "snapshots": 1, "price_changes": 1}  # bad didn't sink good


async def test_run_loop_respects_per_feed_intervals(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    from sportsdata_agents.operations.ingestion.normalizers import normalize_nba_odds

    fast = Feed(name="fast", tool="nba_odds_today", mcp_groups=("nba",), normalizer=normalize_nba_odds,
                interval_s=60)
    slow = Feed(name="slow", tool="nba_odds_today", mcp_groups=("nba",), normalizer=normalize_nba_odds,
                interval_s=300)
    manager = FakeManager({"nba_odds_today": NBA_MINI})

    clock = {"now": T0}

    def now() -> dt.datetime:
        return clock["now"]

    async def sleep(seconds: float) -> None:
        clock["now"] = clock["now"] + dt.timedelta(seconds=seconds)

    await run_loop(manager, db_sessionmaker, [fast, slow], now=now, sleep=sleep, max_cycles=4)
    # cycle 1: both due at T0 → 2 calls; then fast every 60s, slow not due again
    # until +300s — 4 bounded cycles must call fast more often than slow.
    assert manager.calls.count("nba_odds_today") >= 5


async def test_snapshot_start_time_parsed_from_meta(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """B3: the advertised start (ISO or epoch, sports or racing key) lands in the
    start_time column so the resolver can window futures on their REAL day."""
    iso = PricePoint(provider="p", book="B", sport="afl", event_external_id="E1",
                     market="h2h", selection="home", odds=1.9,
                     meta={"start_time": "2026-09-26T04:40:00Z"})
    epoch = PricePoint(provider="p", book="B", sport="afl", event_external_id="E2",
                       market="h2h", selection="home", odds=1.9,
                       meta={"start_time": 1789983000})
    racing = PricePoint(provider="p", book="B", sport="horse_racing", event_external_id="E3",
                        market="win", selection="1", odds=4.5,
                        meta={"post_time": "2026-06-16T06:00:00.000Z"})
    unparseable = PricePoint(provider="p", book="B", sport="afl", event_external_id="E4",
                             market="h2h", selection="home", odds=1.9,
                             meta={"start_time": "soon-ish"})
    await record_points(db_sessionmaker, [iso, epoch, racing, unparseable])
    async with db_sessionmaker() as s:
        rows = {r.event_external_id: r.start_time
                for r in (await s.execute(select(OddsSnapshot))).scalars()}
    assert rows["E1"] is not None and rows["E1"].year == 2026 and rows["E1"].month == 9
    assert rows["E2"] is not None and rows["E2"].year == 2026
    assert rows["E3"] is not None and rows["E3"].day == 16
    assert rows["E4"] is None  # junk degrades to capture-day windowing, never crashes


async def test_futures_fetchers_compose_listing_then_prices() -> None:
    """B10/B11: each futures fetcher walks listing → per-event price route and
    packages payloads in the shape its (reused) normalizer expects."""
    from sportsdata_agents.operations.ingestion.fetchers import (
        fetch_pointsbet_racing_futures,
        fetch_sportsbet_all,
        fetch_sportsbet_racing_futures,
        fetch_tab_racing_futures,
        fetch_unibet_all,
    )

    sb = FakeManager({
        "sportsbet_racing_futures": [
            {"id": 10383894, "bettingStatus": "PRICED", "name": "Tatts Tiara - Win Or Place",
             "className": "Horse Racing: Futures - AUS/NZ", "startTime": 1782484200},
            {"id": 2, "bettingStatus": "SUSPENDED", "name": "skip me"},
        ],
        "sportsbet_event_markets": [{"name": "Win or Place", "selections": []}],
    })
    out = await fetch_sportsbet_racing_futures(sb)
    assert out["events"][0]["sport"] == "horse_racing"
    assert out["events"][0]["event_name"] == "Tatts Tiara - Win Or Place"
    assert sb.calls.count("sportsbet_event_markets") == 1  # suspended one skipped

    tab = FakeManager({
        "tab_racing_futures_meetings": {"meetings": [{
            "meetingName": "Racing Futures", "raceType": "R",
            "races": [{"raceName": "Queen Anne Stakes (All In)", "meetingDate": "2026-06-16",
                       "raceStartTime": "2026-06-16T06:00:00.000Z"}],
        }]},
        "tab_racing_futures_race": {"raceName": "Queen Anne Stakes (All In)", "runners": []},
    })
    out = await fetch_tab_racing_futures(tab)
    assert out["races"][0]["summary"]["raceName"] == "Queen Anne Stakes (All In)"
    assert out["races"][0]["summary"]["meeting"]["venueMnemonic"] == "Racing Futures"

    pb = FakeManager({
        "pointsbet_racing_futures": {"events": [{"key": "2737847"}]},
        "pointsbet_event": {"key": "2737847", "fixedOddsMarkets": []},
    })
    out = await fetch_pointsbet_racing_futures(pb)
    assert out["events"][0]["key"] == "2737847"

    # B9/B10 discovery routes call BOTH the match and outright listings
    ub = FakeManager({
        "unibet_kambi_call": {"group": {"groups": [{"termKey": "australian_rules", "boCount": 3}]},
                              "events": []},
    })
    await fetch_unibet_all(ub)
    assert ub.calls.count("unibet_kambi_call") == 3  # group + matches + competitions

    nav = {"id": 1, "idType": "class", "name": "AFL",
           "navItems": [{"id": 6136, "idType": "competition", "name": "AFL Brownlow Medal"}]}
    sba = FakeManager({
        "sportsbet_nav_hierarchy": nav,
        "sportsbet_competition_matches": [],
        "sportsbet_competition_outrights": [],
    })
    await fetch_sportsbet_all(sba)
    assert sba.calls.count("sportsbet_competition_matches") == 1
    assert sba.calls.count("sportsbet_competition_outrights") == 1


async def test_entain_categories_discovered_with_fallback() -> None:
    """Categories come from the SportingCategories op (non-sports excluded);
    a rejecting gateway degrades to the documented snapshot, never to nothing."""
    from sportsdata_agents.operations.ingestion.fetchers import (
        ENTAIN_SPORT_CATEGORIES,
        fetch_entain_all,
    )

    discovering = FakeManager({
        "entain_graphql_call": {"data": {"categories": [
            {"id": "uuid-bb", "name": "Basketball", "category": "BASKETBALL"},
            {"id": "uuid-nov", "name": "Novelty", "category": "NOVELTY"},  # excluded
        ]}},
        "entain_sport_event_request": {"events": {}},
    })
    await fetch_entain_all(discovering)
    assert discovering.calls.count("entain_sport_event_request") == 1  # basketball only

    broken = FakeManager({
        "entain_graphql_call": RuntimeError("PERSISTED_QUERY_NOT_FOUND"),
        "entain_sport_event_request": {"events": {}},
    })
    await fetch_entain_all(broken)
    assert broken.calls.count("entain_sport_event_request") == len(ENTAIN_SPORT_CATEGORIES)


async def test_line_monitor_fires_and_dedupes(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """M3.2 exit gate (offline leg): a configured watch fires a push alert on a
    real line move; the same condition does NOT refire next cycle; the cursor
    advances so the watch is resumable."""
    from sportsdata_agents.data.models import Alert, Subscription
    from sportsdata_agents.operations.monitoring import run_watches

    await record_points(db_sessionmaker, [_point(2.00)], captured_at=T0)
    await record_points(db_sessionmaker, [_point(1.70)], captured_at=T1)  # -15%

    pushed: list[str] = []

    async def pusher(sub: Any, message: str) -> bool:
        pushed.append(message)
        return True

    async with db_sessionmaker() as s:
        s.add(Subscription(tenant_id="t", workspace_id="w", name="big-moves",
                           kind="line_move", params={"threshold_pct": 10}, channel="log"))
        await s.commit()

    report = await run_watches(db_sessionmaker, pusher=pusher, now=T2)
    assert report["alerts"] == 1 and len(pushed) == 1
    assert "2.00 → 1.70" in pushed[0] and "shortened" in pushed[0]
    async with db_sessionmaker() as s:
        alert = (await s.execute(select(Alert))).scalar_one()
        assert alert.pushed is True and alert.kind == "line_move"
        sub = (await s.execute(select(Subscription))).scalar_one()
        cursor = sub.cursor.replace(tzinfo=dt.UTC) if sub.cursor.tzinfo is None else sub.cursor
        assert cursor == T2  # durable/resumable (SQLite drops tzinfo)

    # same condition, next cycle: deduped AND behind the cursor — no refire
    report = await run_watches(db_sessionmaker, pusher=pusher, now=T2 + dt.timedelta(minutes=5))
    assert report["alerts"] == 0 and len(pushed) == 1


async def test_steam_and_value_watches(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from sportsdata_agents.data.models import ModelArtifact, Prediction, Subscription
    from sportsdata_agents.operations.monitoring import run_watches

    # three same-direction moves = steam
    for i, odds in enumerate((2.00, 1.90, 1.80, 1.70)):
        await record_points(db_sessionmaker, [_point(odds, "away")],
                            captured_at=T0 + dt.timedelta(minutes=i))
    pushed: list[str] = []

    async def pusher(sub: Any, message: str) -> bool:
        pushed.append(message)
        return True

    async with db_sessionmaker() as s:
        s.add(Subscription(tenant_id="t", workspace_id="w", name="steam",
                           kind="steam", params={"min_moves": 3}, channel="log"))
        model = ModelArtifact(tenant_id="t", workspace_id="w", name="m", sport="nba",
                              calibration={"brier": 0.2})
        s.add(model)
        await s.flush()
        # model says 60% at current 1.70... no edge; use higher prob for value
        s.add(Prediction(tenant_id="t", workspace_id="w", model_id=model.id,
                         provider="nba_cdn", event_external_id="0042500403",
                         market="2way", selection="away", prob=0.70))
        s.add(Subscription(tenant_id="t", workspace_id="w", name="edges",
                           kind="value", params={"min_edge_pct": 3}, channel="log"))
        await s.commit()

    report = await run_watches(db_sessionmaker, pusher=pusher, now=T2)
    kinds = {m.split(" — ")[0] for m in pushed}
    assert report["alerts"] == 2  # one steam + one value (0.70*1.70 = +19% edge)
    assert any("steam" in k for k in kinds) and any("value" in k for k in kinds)
