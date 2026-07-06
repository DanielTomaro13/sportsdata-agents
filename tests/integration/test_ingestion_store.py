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
    """The M2.1 exit gate: three captures, one price move → snapshots keep one
    row per price LEVEL (an unchanged capture refreshes captured_at in place),
    prices keep the change-points, the movement query shows them."""
    r0 = await record_points(db_sessionmaker, [_point(1.85), _point(2.00, "away")], captured_at=T0)
    r1 = await record_points(db_sessionmaker, [_point(1.85), _point(2.00, "away")], captured_at=T1)  # unchanged
    r2 = await record_points(db_sessionmaker, [_point(1.92), _point(2.00, "away")], captured_at=T2)  # home moves
    assert (r0["refreshed"], r1["refreshed"], r2["refreshed"]) == (0, 2, 1)

    async with db_sessionmaker() as s:
        # home: a 1.85 row + a 1.92 row; away: ONE 2.00 row, refreshed twice
        assert (await s.execute(select(func.count()).select_from(OddsSnapshot))).scalar_one() == 3
        assert (await s.execute(select(func.count()).select_from(Price))).scalar_one() == 3  # 2 first + 1 move
        # the unchanged away row carries the LATEST confirmation time — the
        # staleness gates read captured_at as "when was this price last alive"
        away_at = (await s.execute(
            select(OddsSnapshot.captured_at).where(OddsSnapshot.selection == "away")
        )).scalar_one()
        assert away_at.replace(tzinfo=dt.UTC) == T2

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
    good = {k: v for k, v in report["good"].items() if not k.endswith("_s")}
    assert good == {"ok": True, "snapshots": 1, "price_changes": 1,
                    "refreshed": 0}  # bad didn't sink good


async def test_ingest_once_fetches_feeds_in_parallel(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Feeds overlap on the wire: a tick's wall clock is the slowest feed, not
    the sum (sequential ticks left every book stale behind the slow one, and
    the cross-book scans read that asymmetry as edge)."""
    import asyncio

    from sportsdata_agents.operations.ingestion.normalizers import normalize_nba_odds

    in_flight, peak = 0, 0

    class SlowManager:
        async def call_tool(self, tool: str, args: dict) -> object:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.05)
            in_flight -= 1
            return NBA_MINI

    feeds = [Feed(name=f"feed{i}", tool="nba_odds_today", mcp_groups=("nba",),
                  normalizer=normalize_nba_odds) for i in range(4)]
    report = await ingest_once(SlowManager(), db_sessionmaker, feeds)
    assert all(report[f.name]["ok"] for f in feeds)
    assert peak >= 3, f"fetches did not overlap (peak concurrency {peak})"


def test_tuned_feeds_priority_and_overrides(monkeypatch) -> None:
    """Cadence is the OPERATOR'S dial: sharps default to the hot tier, and an
    explicit per-feed map is the final word."""
    from sportsdata_agents.operations.ingestion.worker import FEEDS, tuned_feeds

    by_name = {f.name: f for f in tuned_feeds()}
    # sharps ride the priority tier by default
    assert by_name["betfair_all"].interval_s == 60
    assert by_name["pinnacle_all"].interval_s == 60
    # a custom priority list + explicit override both apply
    monkeypatch.setenv("SPORTSDATA_AGENTS_PRIORITY_FEEDS", "tab_all")
    monkeypatch.setenv("SPORTSDATA_AGENTS_PRIORITY_INTERVAL_S", "45")
    monkeypatch.setenv("SPORTSDATA_AGENTS_FEED_INTERVALS", '{"unibet_all": 999}')
    by_name = {f.name: f for f in tuned_feeds()}
    assert by_name["tab_all"].interval_s == 45
    assert by_name["unibet_all"].interval_s == 999
    # non-priority feeds keep their spec'd cadence
    assert by_name["pinnacle_all"].interval_s == FEEDS["pinnacle_all"].interval_s


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


async def test_alert_shows_other_books_for_the_same_market(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A line-move alert quotes the OTHER books mapped to the same fixture —
    including an orientation flip (the other book lists the teams reversed,
    so its AWAY price is quoted for our HOME selection)."""
    from sportsdata_agents.data.models import Subscription
    from sportsdata_agents.operations.ingestion.normalizers import PricePoint
    from sportsdata_agents.operations.monitoring import run_watches
    from sportsdata_agents.operations.resolution import resolve_events

    start = {"start_time": "2026-06-10T09:30:00Z"}
    await record_points(db_sessionmaker, [
        PricePoint(provider="sportsbet", book="Sportsbet", sport="afl", event_external_id="SB1",
                   event_name="Western Bulldogs v Adelaide Crows", market="h2h",
                   selection="home", odds=2.00, meta=start),
        # TAB lists the teams the other way round: its AWAY is Sportsbet's home
        PricePoint(provider="tab", book="TAB", sport="afl", event_external_id="T1",
                   event_name="Adelaide v Wst Bulldogs", market="h2h",
                   selection="away", odds=1.95, meta=start),
    ], captured_at=T0)
    assert (await resolve_events(db_sessionmaker))["created"] == 1
    await record_points(db_sessionmaker, [
        PricePoint(provider="sportsbet", book="Sportsbet", sport="afl", event_external_id="SB1",
                   event_name="Western Bulldogs v Adelaide Crows", market="h2h",
                   selection="home", odds=1.70, meta=start),  # -15% move
    ], captured_at=T1)

    pushed: list[str] = []

    async def pusher(sub: Any, message: str) -> bool:
        pushed.append(message)
        return True

    async with db_sessionmaker() as s:
        s.add(Subscription(tenant_id="t", workspace_id="w", name="moves",
                           kind="line_move", params={"threshold_pct": 10}, channel="log"))
        await s.commit()
    report = await run_watches(db_sessionmaker, pusher=pusher, now=T2)
    assert report["alerts"] == 1
    assert "Western Bulldogs v Adelaide Crows" in pushed[0]
    assert "across books: TAB 1.95" in pushed[0]  # orientation-translated quote


async def test_migrate_warehouse_roundtrip(
    db_sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Any
) -> None:
    """P3: the SQLite -> Postgres mover, proven on SQLite -> SQLite — every row
    copies in FK order; a non-empty target is refused without the override."""
    import pytest as _pytest
    from sqlalchemy.ext.asyncio import async_sessionmaker as _asm
    from sqlalchemy.ext.asyncio import create_async_engine

    from sportsdata_agents.data.base import Base
    from sportsdata_agents.operations.migrate import migrate_warehouse

    # file-based source (the fixture is in-memory; the mover opens fresh engines)
    source_url = f"sqlite+aiosqlite:///{tmp_path}/source.db"
    target_url = f"sqlite+aiosqlite:///{tmp_path}/target.db"
    source_engine = create_async_engine(source_url)
    async with source_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    source_sf = _asm(source_engine, expire_on_commit=False)
    await record_points(source_sf, [_point(1.85), _point(2.00, "away")], captured_at=T0)
    await record_points(source_sf, [_point(1.92)], captured_at=T1)
    await source_engine.dispose()

    report = await migrate_warehouse(source_url, target_url)
    assert report["odds_snapshots"] == 3 and report["prices"] == 3
    assert report["total"] >= 6

    from sqlalchemy import func as _func
    from sqlalchemy import select as _select
    from sqlalchemy.ext.asyncio import create_async_engine

    from sportsdata_agents.data.models import OddsSnapshot as _Snap

    target = create_async_engine(target_url)
    try:
        async with target.connect() as conn:
            count = (await conn.execute(_select(_func.count()).select_from(_Snap))).scalar_one()
        assert count == 3
    finally:
        await target.dispose()

    with _pytest.raises(RuntimeError, match="already has"):
        await migrate_warehouse(source_url, target_url)
    report = await migrate_warehouse(source_url, target_url, allow_nonempty=True)
    assert report["odds_snapshots"] == 3


async def test_feed_health_exact_provider_matching(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Audit fix: a dead tab_racing must NOT hide behind fresh `tab` sports
    captures — staleness matches the feed's exact provider string."""
    from sportsdata_agents.operations.ingestion import FEEDS
    from sportsdata_agents.operations.ingestion.normalizers import PricePoint
    from sportsdata_agents.tools.ops import ops_tools

    assert all(f.provider for f in FEEDS.values())  # every feed declares one
    now = dt.datetime.now(dt.UTC)
    await record_points(db_sessionmaker, [  # tab SPORTS fresh, tab RACING old
        PricePoint(provider="tab", book="TAB", sport="afl", event_external_id="T1",
                   event_name="A v B", market="h2h", selection="home", odds=1.9),
    ], captured_at=now)
    await record_points(db_sessionmaker, [
        PricePoint(provider="tab_racing", book="TAB", sport="horse_racing",
                   event_external_id="R1", event_name="X R1", market="win",
                   selection="1", odds=3.0),
    ], captured_at=now - dt.timedelta(hours=2))

    tools = {t.name: t for t in ops_tools(db_sessionmaker)}
    health = await tools["feed_health"].execute({"hours": 6})
    stale_feeds = {s["feed"] for s in health["stale_feeds"]}
    assert "tab_racing" in stale_feeds  # 2h silent > 3x180s — caught now
    assert "tab_all" not in stale_feeds and "tab_books" not in stale_feeds


async def test_digest_watch_batches_pushes(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A digest watch writes alert rows silently and pushes ONE summary when
    digest_hours elapse."""
    from sportsdata_agents.data.models import Alert, Subscription
    from sportsdata_agents.operations.monitoring import run_watches

    await record_points(db_sessionmaker, [_point(2.00), _point(3.00, "away")], captured_at=T0)
    await record_points(db_sessionmaker, [_point(1.60), _point(4.00, "away")], captured_at=T1)

    pushed: list[str] = []

    async def pusher(sub: Any, message: str) -> bool:
        pushed.append(message)
        return True

    async with db_sessionmaker() as s:
        s.add(Subscription(tenant_id="t", workspace_id="w", name="quiet",
                           kind="line_move",
                           params={"threshold_pct": 10, "digest_hours": 24}, channel="log"))
        await s.commit()

    # first pass: alerts recorded, the digest fires once with both, nothing per-alert
    report = await run_watches(db_sessionmaker, pusher=pusher, now=T2)
    assert report["alerts"] == 2 and report.get("digests") == 1
    assert len(pushed) == 1 and "digest — quiet: 2 alerts" in pushed[0]
    async with db_sessionmaker() as s:
        assert all(a.pushed for a in (await s.execute(select(Alert))).scalars())

    # within the digest window: nothing new pushes
    report = await run_watches(db_sessionmaker, pusher=pusher, now=T2 + dt.timedelta(hours=1))
    assert len(pushed) == 1


async def test_arb_watch_fires_on_cross_book_board(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """An arb watch detects a complete two-way board priced under 1 across two
    books (orientation-flipped), pushes one alert with the stake split, and
    dedupes the same board next cycle."""
    from sportsdata_agents.data.models import Alert, Subscription
    from sportsdata_agents.operations.ingestion.normalizers import PricePoint
    from sportsdata_agents.operations.monitoring import run_watches
    from sportsdata_agents.operations.resolution import resolve_events

    start = {"start_time": "2026-06-10T09:30:00Z"}
    await record_points(db_sessionmaker, [
        PricePoint(provider="sportsbet", book="Sportsbet", sport="afl", event_external_id="SB9",
                   event_name="Western Bulldogs v Adelaide Crows", market="h2h",
                   selection="home", odds=2.10, meta=start),
        PricePoint(provider="sportsbet", book="Sportsbet", sport="afl", event_external_id="SB9",
                   event_name="Western Bulldogs v Adelaide Crows", market="h2h",
                   selection="away", odds=1.70, meta=start),
        # TAB lists the teams reversed: its "home" is the fixture's away
        PricePoint(provider="tab", book="TAB", sport="afl", event_external_id="T9",
                   event_name="Adelaide v Wst Bulldogs", market="h2h",
                   selection="home", odds=2.05, meta=start),
        PricePoint(provider="tab", book="TAB", sport="afl", event_external_id="T9",
                   event_name="Adelaide v Wst Bulldogs", market="h2h",
                   selection="away", odds=1.75, meta=start),
    ], captured_at=T0)
    assert (await resolve_events(db_sessionmaker))["created"] == 1

    pushed: list[str] = []

    async def pusher(sub: Any, message: str) -> bool:
        pushed.append(message)
        return True

    async with db_sessionmaker() as s:
        # hours generous: the seeded captures sit at a fixed test instant
        s.add(Subscription(tenant_id="t", workspace_id="w", name="arbs", kind="arb",
                           params={"threshold_pct": 1.0, "hours": 24 * 365 * 10},
                           channel="log"))
        await s.commit()

    report = await run_watches(db_sessionmaker, pusher=pusher, now=T2)
    # best home 2.10 (Sportsbet) + best away 2.05 (TAB flipped) → 1/2.10+1/2.05 ≈ 0.964
    assert report["alerts"] == 1 and len(pushed) == 1
    assert "ARB" in pushed[0] and "Western Bulldogs v Adelaide Crows" in pushed[0]
    assert "Sportsbet 2.10" in pushed[0] and "TAB 2.05" in pushed[0]
    assert "verify every leg" in pushed[0]
    async with db_sessionmaker() as s:
        alert = (await s.execute(select(Alert))).scalar_one()
        assert alert.kind == "arb" and alert.payload["margin_pct"] > 3

    # same board, next cycle: deduped, no refire
    report = await run_watches(db_sessionmaker, pusher=pusher, now=T2 + dt.timedelta(minutes=5))
    assert report["alerts"] == 0 and len(pushed) == 1


async def test_arb_watch_skips_started_events(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """One pre-game leg + one in-play leg fakes a monster margin no one can take —
    started fixtures never alert."""
    from sportsdata_agents.data.models import Subscription
    from sportsdata_agents.operations.ingestion.normalizers import PricePoint
    from sportsdata_agents.operations.monitoring import run_watches
    from sportsdata_agents.operations.resolution import resolve_events

    start = {"start_time": "2026-06-10T08:00:00Z"}  # before `now` (T2 = 09:10)
    await record_points(db_sessionmaker, [
        PricePoint(provider="sportsbet", book="Sportsbet", sport="afl", event_external_id="SB10",
                   event_name="Geelong Cats v Hawthorn", market="h2h",
                   selection="home", odds=9.0, meta=start),  # in-play crash
        PricePoint(provider="tab", book="TAB", sport="afl", event_external_id="T10",
                   event_name="Geelong Cats v Hawthorn", market="h2h",
                   selection="away", odds=2.0, meta=start),  # pre-game capture
    ], captured_at=T0)
    assert (await resolve_events(db_sessionmaker))["created"] == 1

    pushed: list[str] = []

    async def pusher(sub: Any, message: str) -> bool:
        pushed.append(message)
        return True

    async with db_sessionmaker() as s:
        s.add(Subscription(tenant_id="t", workspace_id="w", name="arbs", kind="arb",
                           params={"threshold_pct": 1.0, "hours": 24 * 365 * 10},
                           channel="log"))
        await s.commit()
    report = await run_watches(db_sessionmaker, pusher=pusher, now=T2)
    assert report["alerts"] == 0 and pushed == []


async def test_arb_alert_outcome_is_remeasured(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The honesty loop: 5+ minutes after an arb alert fires, the SAME board is
    re-measured and the outcome stamped into the payload — including when the
    margin has decayed away."""
    from sportsdata_agents.data.models import Alert, Subscription
    from sportsdata_agents.operations.ingestion.normalizers import PricePoint
    from sportsdata_agents.operations.monitoring import run_watches
    from sportsdata_agents.operations.resolution import resolve_events

    start = {"start_time": "2026-06-10T11:30:00Z"}

    def board(home: float, away_flipped: float, at: Any) -> Any:
        return record_points(db_sessionmaker, [
            PricePoint(provider="sportsbet", book="Sportsbet", sport="afl",
                       event_external_id="SB20", event_name="Carlton v Essendon",
                       market="h2h", selection="home", odds=home, meta=start),
            PricePoint(provider="sportsbet", book="Sportsbet", sport="afl",
                       event_external_id="SB20", event_name="Carlton v Essendon",
                       market="h2h", selection="away", odds=1.70, meta=start),
            PricePoint(provider="tab", book="TAB", sport="afl",
                       event_external_id="T20", event_name="Essendon v Carlton",
                       market="h2h", selection="home", odds=away_flipped, meta=start),
            PricePoint(provider="tab", book="TAB", sport="afl",
                       event_external_id="T20", event_name="Essendon v Carlton",
                       market="h2h", selection="away", odds=1.75, meta=start),
        ], captured_at=at)

    await board(2.10, 2.05, T0)  # 1/2.10 + 1/2.05 ≈ 0.964 — a 3.6% arb
    assert (await resolve_events(db_sessionmaker))["created"] == 1

    async def pusher(sub: Any, message: str) -> bool:
        return True

    async with db_sessionmaker() as s:
        s.add(Subscription(tenant_id="t", workspace_id="w", name="arbs", kind="arb",
                           params={"threshold_pct": 1.0, "hours": 24 * 365 * 10},
                           channel="log"))
        await s.commit()
    report = await run_watches(db_sessionmaker, pusher=pusher, now=T2)
    assert report["alerts"] == 1 and report["outcomes_measured"] == 0  # too fresh
    async with db_sessionmaker() as s:
        alert = (await s.execute(select(Alert).where(Alert.kind == "arb"))).scalar_one()
        alert.created_at = T2  # the column defaults to REAL now; the test drives a synthetic clock
        await s.commit()

    # the board decays before the re-measurement window
    await board(1.90, 1.85, T2 + dt.timedelta(minutes=2))
    report = await run_watches(db_sessionmaker, pusher=pusher,
                               now=T2 + dt.timedelta(minutes=6))
    assert report["outcomes_measured"] == 1
    async with db_sessionmaker() as s:
        alert = (await s.execute(select(Alert).where(Alert.kind == "arb"))).scalar_one()
        outcome = alert.payload["outcome"]
        assert outcome["still_arb"] is False  # 1/1.90 + 1/1.85 > 1 — margin gone
        assert outcome["margin_pct_after"] < 0
    # already measured: never re-stamped
    report = await run_watches(db_sessionmaker, pusher=pusher,
                               now=T2 + dt.timedelta(minutes=10))
    assert report["outcomes_measured"] == 0


async def test_value_alert_outcome_is_remeasured(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The honesty loop covers value alerts too: the edge is re-checked at the
    CURRENT listed price 5+ minutes after firing."""
    from sportsdata_agents.data.models import Alert, ModelArtifact, Prediction, Subscription
    from sportsdata_agents.operations.monitoring import run_watches

    await record_points(db_sessionmaker, [_point(1.70, "away")], captured_at=T0)

    async def pusher(sub: Any, message: str) -> bool:
        return True

    async with db_sessionmaker() as s:
        model = ModelArtifact(tenant_id="t", workspace_id="w", name="m", sport="nba",
                              calibration={"brier": 0.2})
        s.add(model)
        await s.flush()
        s.add(Prediction(tenant_id="t", workspace_id="w", model_id=model.id,
                         provider="nba_cdn", event_external_id="0042500403",
                         market="2way", selection="away", prob=0.70))
        s.add(Subscription(tenant_id="t", workspace_id="w", name="edges",
                           kind="value", params={"min_edge_pct": 3}, channel="log"))
        await s.commit()

    report = await run_watches(db_sessionmaker, pusher=pusher, now=T2)
    assert report["alerts"] == 1  # 0.70 * 1.70 = +19% edge
    async with db_sessionmaker() as s:
        alert = (await s.execute(select(Alert).where(Alert.kind == "value"))).scalar_one()
        assert alert.payload["prob"] == 0.7  # enriched for the re-probe
        alert.created_at = T2
        await s.commit()

    # the price collapses before the re-measurement window
    await record_points(db_sessionmaker, [_point(1.30, "away")],
                        captured_at=T2 + dt.timedelta(minutes=2))
    report = await run_watches(db_sessionmaker, pusher=pusher,
                               now=T2 + dt.timedelta(minutes=6))
    assert report["outcomes_measured"] == 1
    async with db_sessionmaker() as s:
        alert = (await s.execute(select(Alert).where(Alert.kind == "value"))).scalar_one()
        outcome = alert.payload["outcome"]
        assert outcome["still_value"] is False  # 0.70 * 1.30 = -9% — edge gone
        assert outcome["edge_pct_after"] == pytest.approx(-9.0, abs=0.1)


async def test_same_day_doubleheaders_never_merge(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """MLB doubleheaders: identical teams, same day, starts hours apart are
    DIFFERENT games — two fixtures; books advertising the same start still merge."""
    from sportsdata_agents.data.models import Fixture
    from sportsdata_agents.operations.ingestion.normalizers import PricePoint
    from sportsdata_agents.operations.resolution import resolve_events

    def pt(provider: str, book: str, ext: str, start_iso: str) -> PricePoint:
        return PricePoint(provider=provider, book=book, sport="baseball",
                          event_external_id=ext, event_name="Mariners v Orioles",
                          market="h2h", selection="home", odds=1.9,
                          meta={"start_time": start_iso})

    await record_points(db_sessionmaker, [
        pt("tab", "TAB", "G1", "2026-06-10T17:00:00Z"),         # game 1
        pt("sportsbet", "Sportsbet", "SB-G1", "2026-06-10T17:05:00Z"),  # same game, 5min apart
        pt("tab", "TAB", "G2", "2026-06-10T23:30:00Z"),         # game 2: 6.5h later
    ], captured_at=T0)
    report = await resolve_events(db_sessionmaker)
    assert report["created"] == 2  # two games, not one
    async with db_sessionmaker() as s:
        fixtures = (await s.execute(select(Fixture))).scalars().all()
    assert len(fixtures) == 2
