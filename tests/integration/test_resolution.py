"""Resolution milestone: dictionary-as-data, the steward's safety rails, event
resolution across books, cross-book queries, racing results."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import Event, EventResult, Fixture
from sportsdata_agents.operations.ingestion import PricePoint, record_points
from sportsdata_agents.operations.ingestion.normalizers import (
    canonical_market,
    canonical_sport,
    reload_dictionary,
)
from sportsdata_agents.operations.resolution import cross_book_prices, resolve_events, split_sides
from sportsdata_agents.tools.dictionary import dictionary_tools

pytestmark = pytest.mark.integration

T0 = dt.datetime(2026, 6, 11, 9, 0, tzinfo=dt.UTC)


# ── dictionary as data ────────────────────────────────────────────────────


def test_dictionary_loads_from_packaged_data() -> None:
    assert canonical_market("Match Result") == "h2h"
    assert canonical_market("MONEY_LINE") == "h2h"
    assert canonical_market("Pick Your Own Line") == "spread"
    assert canonical_market("Exact Winning Margin") == "exact winning margin"  # unmapped flows through
    assert canonical_sport("NRL") == "rugby_league"
    assert canonical_sport("AFL Football") == "australian_rules"
    assert canonical_sport("quidditch") == "quidditch"


async def test_overrides_extend_without_code(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    overrides = tmp_path / "dict.local.json"
    overrides.write_text('{"markets": {"h2h": ["straight up winner"]}, "sports": {"soccer": ["epl"]}}')
    monkeypatch.setenv("SPORTSDATA_AGENTS_DICTIONARY_OVERRIDES", str(overrides))
    reload_dictionary()
    try:
        assert canonical_market("Straight Up Winner") == "h2h"
        assert canonical_sport("EPL") == "soccer"
    finally:
        monkeypatch.delenv("SPORTSDATA_AGENTS_DICTIONARY_OVERRIDES")
        reload_dictionary()
    assert canonical_market("straight up winner") == "straight up winner"  # gone with the override file


async def test_steward_tools_enforce_merge_safety(
    db_sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPORTSDATA_AGENTS_DICTIONARY_OVERRIDES", str(tmp_path / "ov.json"))
    reload_dictionary()
    tools = {t.name: t for t in dictionary_tools(db_sessionmaker)}
    try:
        # qualifier names can NEVER merge into a base family — enforced in code
        with pytest.raises(ValueError, match="qualifier"):
            await tools["add_market_alias"].execute(
                {"family": "h2h", "alias": "1st half head to head"}
            )
        # …but they may found their own family
        out = await tools["add_market_alias"].execute(
            {"family": "h2h 1st half", "alias": "1st half head to head",
             "rationale": "half-scoped h2h across books"}
        )
        assert out["added"] is True
        assert canonical_market("1st Half Head To Head") == "h2h 1st half"
        # remapping an alias that already belongs elsewhere is refused
        with pytest.raises(ValueError, match="already mapped"):
            await tools["add_market_alias"].execute({"family": "total", "alias": "match result"})
        removed = await tools["remove_market_alias"].execute({"alias": "1st half head to head"})
        assert removed["removed"] is True
    finally:
        monkeypatch.delenv("SPORTSDATA_AGENTS_DICTIONARY_OVERRIDES")
        reload_dictionary()


# ── event resolution ──────────────────────────────────────────────────────


def test_split_sides_handles_every_separator() -> None:
    assert split_sides("Western Bulldogs v Adelaide Crows") == ("Western Bulldogs", "Adelaide Crows")
    assert split_sides("Gold Coast Suns vs Hawthorn") == ("Gold Coast Suns", "Hawthorn")
    assert split_sides("Western Bulldogs - Adelaide") == ("Western Bulldogs", "Adelaide")
    # US '@'/'At' list away first — normalised to (home, away)
    assert split_sides("San Antonio Spurs @ New York Knicks") == ("New York Knicks", "San Antonio Spurs")
    assert split_sides("New York Knicks At San Antonio Spurs") == (
        "San Antonio Spurs", "New York Knicks")  # sportsbet's US-league naming, live-captured
    assert split_sides("Finger Lakes R8") is None  # racing names are one-sided


def _pt(provider: str, book: str, event_id: str, name: str, sport: str, odds: float) -> PricePoint:
    return PricePoint(provider=provider, book=book, sport=sport, event_external_id=event_id,
                      event_name=name, market="h2h", selection="home", odds=odds)


async def test_resolver_joins_four_books_onto_one_fixture(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The same match under four books' names/ids/sport-labels → ONE fixture; an
    unrelated match stays separate; cross_book_prices joins and ranks best-first."""
    await record_points(db_sessionmaker, [
        _pt("sportsbet", "Sportsbet", "10551746", "Western Bulldogs v Adelaide Crows", "afl", 1.73),
        _pt("tab", "TAB", "WBdvAdl", "Wst Bulldogs v Adelaide", "afl", 1.74),
        _pt("entain", "Ladbrokes", "EV-uuid-1", "Western Bulldogs vs Adelaide Crows",
            "australian_rules", 1.75),
        _pt("unibet", "Unibet", "1025627732", "Western Bulldogs - Adelaide", "australian_rules", 1.78),
        _pt("sportsbet", "Sportsbet", "10551755", "St Kilda v GWS GIANTS", "afl", 1.78),
    ], captured_at=T0)

    stats = await resolve_events(db_sessionmaker)
    assert stats == {"examined": 5, "mapped": 5, "created": 2, "ambiguous": 0, "skipped_unnamed": 0}

    async with db_sessionmaker() as s:
        fixtures = (await s.execute(select(Fixture).order_by(Fixture.name))).scalars().all()
        assert len(fixtures) == 2
        bulldogs = next(f for f in fixtures if "Bulldogs" in f.name)
        n_mapped = (await s.execute(
            select(func.count()).select_from(Event).where(Event.fixture_id == bulldogs.id)
        )).scalar_one()
        assert n_mapped == 4  # all four books joined
        assert bulldogs.sport == "australian_rules"  # sport labels canonicalised

    book = await cross_book_prices(db_sessionmaker, fixture_id=str(bulldogs.id))
    assert book["books"] == 4
    homes = book["selections"]["home"]
    assert [h["book"] for h in homes][:2] == ["Unibet", "Ladbrokes"]  # best price first
    assert homes[0]["odds"] == 1.78

    # idempotent: a second pass maps nothing new
    again = await resolve_events(db_sessionmaker)
    assert again["mapped"] == 0 and again["created"] == 0


async def test_resolver_never_guesses_on_ambiguity(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Two same-day fixtures that BOTH match a sloppy name → skipped, not guessed."""
    await record_points(db_sessionmaker, [
        _pt("sportsbet", "Sportsbet", "1", "Sydney Swans v Carlton", "afl", 1.8),
        _pt("sportsbet", "Sportsbet", "2", "Sydney Roosters v Carlton", "afl", 1.9),
    ], captured_at=T0)
    assert (await resolve_events(db_sessionmaker))["created"] == 2
    await record_points(db_sessionmaker, [
        _pt("tab", "TAB", "X1", "Sydney v Carlton", "afl", 1.85),  # matches both
    ], captured_at=T0)
    stats = await resolve_events(db_sessionmaker)
    assert stats["ambiguous"] == 1 and stats["mapped"] == 0


# ── racing results ────────────────────────────────────────────────────────


async def test_racing_results_from_placings(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    from sportsdata_agents.operations.ingestion.results import ingest_racing_results

    class FakeManager:
        async def call_tool(self, name: str, args: Any = None) -> Any:
            assert name == "pointsbet_racing_meetings"
            return [{"meetings": [{"races": [
                {"raceId": "110085943", "placing": "3,8,10,1"},
                {"raceId": "110085944", "placing": ""},  # not run yet
            ]}]}]

    written = await ingest_racing_results(FakeManager(), db_sessionmaker)
    assert written == 1
    async with db_sessionmaker() as s:
        row = (await s.execute(select(EventResult))).scalar_one()
        assert row.event_external_id == "110085943" and row.winning_selection == "3"


# ── fix pass (B1, B3, B4, B7) ─────────────────────────────────────────────


async def test_list_market_names_is_dialect_safe(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """B1: provider aggregation happens in Python — the query must contain no
    SQLite-only functions (this test runs under Postgres in CI too)."""
    await record_points(db_sessionmaker, [
        PricePoint(provider="sportsbet", book="Sportsbet", sport="afl", event_external_id="1",
                   event_name="A v B", market="weird exotic", selection="x", odds=2.0),
        PricePoint(provider="tab", book="TAB", sport="afl", event_external_id="2",
                   event_name="A v B", market="weird exotic", selection="y", odds=3.0),
    ], captured_at=T0)
    tools = {t.name: t for t in dictionary_tools(db_sessionmaker)}
    out = await tools["list_market_names"].execute({"only_unmapped": True, "min_count": 1})
    row = next(r for r in out["names"] if r["market"] == "weird exotic")
    assert row["rows"] == 2
    assert row["providers"] == "sportsbet,tab"
    assert row["currently"] == "unmapped"


async def test_steward_guard_covers_qualifier_families(
    db_sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B7 (live evidence): 'spread p1 alt' must NOT merge into 'spread alt' — the
    alias's qualifiers have to appear in the family name, base family or not."""
    monkeypatch.setenv("SPORTSDATA_AGENTS_DICTIONARY_OVERRIDES", str(tmp_path / "ov.json"))
    reload_dictionary()
    tools = {t.name: t for t in dictionary_tools(db_sessionmaker)}
    try:
        with pytest.raises(ValueError, match="qualifier"):
            await tools["add_market_alias"].execute(
                {"family": "spread alt", "alias": "spread p1 alt"}
            )
        out = await tools["add_market_alias"].execute(
            {"family": "spread p1 alt", "alias": "spread alt p1"}  # qualifiers agree
        )
        assert out["added"] is True
    finally:
        monkeypatch.delenv("SPORTSDATA_AGENTS_DICTIONARY_OVERRIDES")
        reload_dictionary()


async def test_resolver_windows_futures_on_advertised_start(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """B3: two books capture the same outright WEEKS apart — the shared advertised
    start (not the capture day) must join them onto one fixture."""
    start = {"start_time": "2026-09-26T04:40:00Z"}
    await record_points(db_sessionmaker, [
        PricePoint(provider="unibet", book="Unibet", sport="australian_rules",
                   event_external_id="K1", event_name="AFL 2026 Premiership Winner",
                   market="premiership winner", selection="adelaide crows", odds=5.0, meta=start),
    ], captured_at=T0)
    await record_points(db_sessionmaker, [
        PricePoint(provider="sportsbet", book="Sportsbet", sport="afl",
                   event_external_id="S1", event_name="AFL 2026 Premiership Winner",
                   market="premiership winner", selection="adelaide crows", odds=5.5, meta=start),
    ], captured_at=T0 + dt.timedelta(days=30))  # a month later
    stats = await resolve_events(db_sessionmaker)
    assert stats["mapped"] == 2 and stats["created"] == 1  # ONE fixture despite 30d gap
    async with db_sessionmaker() as s:
        fixture = (await s.execute(select(Fixture))).scalar_one()
        assert fixture.start_time is not None and fixture.start_time.month == 9


async def test_backtest_settles_across_books_through_fixtures(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """B4: a prediction priced at TAB settles from a result recorded under
    Sportsbet's event id — joined via the shared fixture, side-orientation checked
    (TAB lists the teams in the OPPOSITE order here, so 'home' must flip)."""
    from sportsdata_agents.data.models import ModelArtifact, Prediction
    from sportsdata_agents.data.repository import TenantScope
    from sportsdata_agents.quant.backtest import run_backtest

    await record_points(db_sessionmaker, [
        _pt("sportsbet", "Sportsbet", "SB1", "Western Bulldogs v Adelaide Crows", "afl", 1.73),
        _pt("tab", "TAB", "T1", "Adelaide v Wst Bulldogs", "afl", 2.10),  # swapped listing
    ], captured_at=T0)
    assert (await resolve_events(db_sessionmaker))["created"] == 1

    scope = TenantScope("t", "w")
    async with db_sessionmaker() as s:
        model = ModelArtifact(tenant_id=scope.tenant_id, workspace_id=scope.workspace_id,
                              name="m", sport="afl", calibration={"brier": 0.2})
        s.add(model)
        await s.flush()
        # TAB frame: 'home' is Adelaide (they list Adelaide first)
        s.add(Prediction(tenant_id=scope.tenant_id, workspace_id=scope.workspace_id,
                         model_id=model.id, provider="tab", event_external_id="T1",
                         market="h2h", selection="home", prob=0.60,
                         predicted_at=T0 + dt.timedelta(minutes=1)))
        # result recorded under SPORTSBET's id and frame: Bulldogs (their home) won
        s.add(EventResult(provider="sportsbet", sport="afl", event_external_id="SB1",
                          winning_selection="home", settled_at=T0 + dt.timedelta(hours=3)))
        await s.commit()

    report = await run_backtest(db_sessionmaker, scope, min_edge_pct=0.0)
    assert report["bets"] == 1
    # Bulldogs won = TAB's AWAY side; the TAB 'home' (Adelaide) prediction LOST.
    assert report["per_bet"][0]["won"] is False
    assert report["hit_rate_pct"] == 0.0


async def test_find_fixture_and_best_prices_tools(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """B4 tools: agents find the fixture by sloppy name, then get the cross-book
    board best-first."""
    from sportsdata_agents.tools.resolution import resolution_tools

    await record_points(db_sessionmaker, [
        _pt("sportsbet", "Sportsbet", "SB9", "Western Bulldogs v Adelaide Crows", "afl", 1.73),
        _pt("tab", "TAB", "T9", "Wst Bulldogs v Adelaide", "afl", 1.74),
    ], captured_at=T0)
    await resolve_events(db_sessionmaker)
    tools = {t.name: t for t in resolution_tools(db_sessionmaker)}
    found = await tools["find_fixture"].execute({"query": "bulldogs adelaide"})
    assert found["fixtures"] and found["fixtures"][0]["books"] == 2
    board = await tools["best_prices"].execute({"fixture_id": found["fixtures"][0]["fixture_id"]})
    homes = board["selections"]["home"]
    assert homes[0]["book"] == "TAB" and homes[0]["odds"] == 1.74  # best first


async def test_one_name_events_gate_on_subset_not_jaccard(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Outright names share generic tokens ('Markets', '2026') — plain Jaccard
    merged different countries' World Cup markets. The fuzzy-subset gate keeps
    them apart while still letting 'Queen Anne Stakes' join its longer TAB name."""
    start = {"start_time": "2026-07-19T10:00:00Z"}
    await record_points(db_sessionmaker, [
        PricePoint(provider="unibet", book="Unibet", sport="soccer", event_external_id="U1",
                   event_name="Argentina Markets 2026", market="winner", selection="x",
                   odds=2.0, meta=start),
        PricePoint(provider="unibet", book="Unibet", sport="soccer", event_external_id="U2",
                   event_name="Brazil Markets 2026", market="winner", selection="x",
                   odds=2.0, meta=start),
        PricePoint(provider="tab_racing", book="TAB", sport="horse_racing",
                   event_external_id="T1", event_name="Racing Futures Queen Anne Stakes (All In)",
                   market="win", selection="notable speech", odds=2.5,
                   meta={"post_time": "2026-06-16T13:30:00Z"}),
        PricePoint(provider="unibet_racing", book="Unibet", sport="horse_racing",
                   event_external_id="UQ1", event_name="Queen Anne Stakes",
                   market="win", selection="notable speech", odds=2.6,
                   meta={"post_time": "2026-06-16T13:30:00Z"}),
    ], captured_at=T0)
    stats = await resolve_events(db_sessionmaker)
    assert stats["mapped"] == 4
    async with db_sessionmaker() as s:
        names = [f.name for f in (await s.execute(select(Fixture))).scalars()]
    assert len(names) == 3  # two country markets apart; the two Queen Annes joined
    assert sum("Queen Anne" in n for n in names) == 1


async def test_far_future_events_get_a_wide_day_window(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Books placeholder far-future outright dates and disagree by days — events
    more than a month out window at +/-14d (the name gate still decides)."""
    base = dt.datetime.now(dt.UTC) + dt.timedelta(days=60)
    await record_points(db_sessionmaker, [
        PricePoint(provider="unibet", book="Unibet", sport="australian_rules",
                   event_external_id="W1", event_name="AFL Premiership Winner 2026",
                   market="winner", selection="adelaide crows", odds=5.0,
                   meta={"start_time": base.isoformat()}),
        PricePoint(provider="sportsbet", book="Sportsbet", sport="afl",
                   event_external_id="W2", event_name="AFL Premiership Winner 2026",
                   market="winner", selection="adelaide crows", odds=5.5,
                   meta={"start_time": (base + dt.timedelta(days=10)).isoformat()}),
    ], captured_at=T0)
    stats = await resolve_events(db_sessionmaker)
    assert stats["mapped"] == 2 and stats["created"] == 1  # ten days apart, one fixture


async def test_league_results_map_and_settle_cross_book(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Add 4: scoreboard finals map onto existing fixtures (never founding one) and
    settle book predictions through them — orientation translated from the
    result's meta event_name (scoreboards have no odds snapshots)."""
    from sportsdata_agents.data.models import ModelArtifact, Prediction
    from sportsdata_agents.data.repository import TenantScope
    from sportsdata_agents.operations.ingestion.results import ingest_league_results
    from sportsdata_agents.quant.backtest import run_backtest

    await record_points(db_sessionmaker, [
        PricePoint(provider="sportsbet", book="Sportsbet", sport="afl",
                   event_external_id="SB7", event_name="Adelaide Crows v Geelong Cats",
                   market="h2h", selection="home", odds=1.95,
                   meta={"start_time": "2026-06-04T09:30:00Z"}),
    ], captured_at=dt.datetime(2026, 6, 4, 7, 0, tzinfo=dt.UTC))
    assert (await resolve_events(db_sessionmaker))["created"] == 1

    class FakeManager:
        async def call_tool(self, name: str, args: Any = None) -> Any:
            if name == "nba_scoreboard_today":
                return {"scoreboard": {"games": []}}
            if name == "afl_matches_list":
                return {"matches": [
                    {"providerId": "CD_M1", "status": "CONCLUDED",
                     "compSeason": {"name": "2026 Toyota AFL Premiership"},
                     "utcStartTime": "2026-06-04T09:30:00.000+0000",
                     "home": {"team": {"name": "Adelaide Crows"}, "score": {"totalScore": 75}},
                     "away": {"team": {"name": "Geelong Cats"}, "score": {"totalScore": 74}}},
                    {"providerId": "CD_M2", "status": "SCHEDULED",  # not final -> skipped
                     "home": {"team": {"name": "X"}}, "away": {"team": {"name": "Y"}}},
                ]}
            if name == "nrl_competitions":
                return {"competitionDetails": {"competition": []}}
            if name == "mlb_schedule":
                return {"dates": []}
            if name == "espn_scoreboard":
                return {"events": [
                    {"id": "401", "status": {"type": {"completed": True}},
                     "competitions": [{"date": "2026-06-10T17:10Z", "competitors": [
                         {"homeAway": "home", "score": "7",
                          "team": {"displayName": "Tampa Bay Rays"}},
                         {"homeAway": "away", "score": "5",
                          "team": {"displayName": "Boston Red Sox"}},
                     ]}]},
                    {"id": "402", "status": {"type": {"completed": False}},  # live -> skipped
                     "competitions": [{"competitors": []}]},
                ]}
            raise AssertionError(name)

    from sportsdata_agents.operations.ingestion.results import _ESPN_LEAGUES

    report = await ingest_league_results(FakeManager(), db_sessionmaker)
    assert report["afl"] == 1
    # the fake serves the same one-final board to every catalogued ESPN league
    assert report["espn"] == len(_ESPN_LEAGUES)
    assert report["recorded"] == 2  # AFL final + the ESPN final (repeat ids upsert)
    assert report["fixtures_mapped"] == 1  # joined the Sportsbet fixture

    scope = TenantScope("t", "w")
    async with db_sessionmaker() as s:
        model = ModelArtifact(tenant_id=scope.tenant_id, workspace_id=scope.workspace_id,
                              name="m", sport="afl", calibration={"brier": 0.2})
        s.add(model)
        await s.flush()
        s.add(Prediction(tenant_id=scope.tenant_id, workspace_id=scope.workspace_id,
                         model_id=model.id, provider="sportsbet", event_external_id="SB7",
                         market="h2h", selection="home", prob=0.60,
                         predicted_at=dt.datetime(2026, 6, 4, 8, 0, tzinfo=dt.UTC)))
        await s.commit()
    report = await run_backtest(db_sessionmaker, scope, min_edge_pct=0.0)
    assert report["bets"] == 1 and report["per_bet"][0]["won"] is True  # Crows won at home


async def test_clv_book_benchmarks_against_sharp_close(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Add 6: clv_book uses the benchmark book's close at the same fixture
    (orientation-translated) — falling back to the own close when absent."""
    from sportsdata_agents.data.models import ModelArtifact, Prediction
    from sportsdata_agents.data.repository import TenantScope
    from sportsdata_agents.quant.backtest import run_backtest

    t0 = dt.datetime(2026, 6, 4, 7, 0, tzinfo=dt.UTC)
    start = {"start_time": "2026-06-04T09:30:00Z"}
    await record_points(db_sessionmaker, [
        PricePoint(provider="tab", book="TAB", sport="afl", event_external_id="TB1",
                   event_name="Adelaide v Geelong", market="h2h", selection="home",
                   odds=2.00, meta=start),
        # Pinnacle lists the sides the other way round: its AWAY is TAB's home
        PricePoint(provider="pinnacle", book="Pinnacle", sport="australian_rules",
                   event_external_id="PN1", event_name="Geelong Cats v Adelaide Crows",
                   market="h2h", selection="away", odds=1.90, meta=start),
    ], captured_at=t0)
    await record_points(db_sessionmaker, [
        PricePoint(provider="pinnacle", book="Pinnacle", sport="australian_rules",
                   event_external_id="PN1", event_name="Geelong Cats v Adelaide Crows",
                   market="h2h", selection="away", odds=1.60, meta=start),  # sharp close
    ], captured_at=t0 + dt.timedelta(hours=2))
    await resolve_events(db_sessionmaker)

    scope = TenantScope("t", "w")
    async with db_sessionmaker() as s:
        model = ModelArtifact(tenant_id=scope.tenant_id, workspace_id=scope.workspace_id,
                              name="m", sport="afl", calibration={"brier": 0.2})
        s.add(model)
        await s.flush()
        s.add(Prediction(tenant_id=scope.tenant_id, workspace_id=scope.workspace_id,
                         model_id=model.id, provider="tab", event_external_id="TB1",
                         market="h2h", selection="home", prob=0.60, predicted_at=t0))
        s.add(EventResult(provider="tab", sport="afl", event_external_id="TB1",
                          winning_selection="home", settled_at=t0 + dt.timedelta(hours=4)))
        await s.commit()

    plain = await run_backtest(db_sessionmaker, scope, min_edge_pct=0.0)
    assert plain["per_bet"][0]["clv_pct"] == 0.0  # own close never moved
    sharp = await run_backtest(db_sessionmaker, scope, min_edge_pct=0.0, clv_book="Pinnacle")
    assert sharp["clv_benchmarked_bets"] == 1
    assert sharp["per_bet"][0]["closing_odds"] == 1.60  # entered 2.00 vs sharp close 1.60
    assert sharp["per_bet"][0]["clv_pct"] == 25.0


async def test_result_ids_never_collide_across_providers(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Five result providers share one numeric id namespace: upserts key on
    (provider, id) so neither overwrites the other, and the backtest's direct
    ext-only lookup refuses a collided id (falls to the fixture join)."""
    from sportsdata_agents.operations.ingestion.results import record_results
    from sportsdata_agents.quant.backtest import _settlement_maps

    await record_results(db_sessionmaker, [
        {"provider": "espn", "sport": "basketball", "event_external_id": "400123",
         "winning_selection": "home"},
        {"provider": "pointsbet_racing", "sport": "racing", "event_external_id": "400123",
         "winning_selection": "7"},
    ])
    async with db_sessionmaker() as s:
        rows = (await s.execute(select(EventResult))).scalars().all()
        assert len(rows) == 2  # both survive — no overwrite
        maps = await _settlement_maps(s)
    assert maps.result_by_pe[("espn", "400123")].winning_selection == "home"
    assert maps.result_by_pe[("pointsbet_racing", "400123")].winning_selection == "7"
    assert maps.result_by_ext["400123"] is None  # collided -> direct lookup refuses


def test_variant_teams_never_merge() -> None:
    """A women's / age-grade variant is a DIFFERENT team: the subset rule alone
    can't tell "Blues Women" from "Blues" (both ride the longer name, like
    nicknames do) — found live when a Super Rugby Women's match fixture-merged
    with the men's game and manufactured a 74% "arb"."""
    from sportsdata_agents.operations.resolution.resolver import _side_ok, _tokens

    assert not _side_ok(_tokens("Blues"), _tokens("Blues Women"))
    assert not _side_ok(_tokens("Hurricanes"), _tokens("Hurricanes Women"))
    assert not _side_ok(_tokens("Australia"), _tokens("Australia U20"))
    assert not _side_ok(_tokens("Arsenal"), _tokens("Arsenal Reserves"))
    # both carrying the marker is the SAME (women's) team — still merges
    assert _side_ok(_tokens("Blues Women"), _tokens("Blues Women"))
    assert _side_ok(_tokens("Australia U20"), _tokens("Australia U20"))
    # nicknames and abbreviations still merge (the rule this guard must not break)
    assert _side_ok(_tokens("Adelaide"), _tokens("Adelaide Crows"))
    assert _side_ok(_tokens("Wst Bulldogs"), _tokens("Western Bulldogs"))


def test_initials_name_the_same_side() -> None:
    """TAB abbreviates rep sides — "NSW" IS "New South Wales" (lived: State of
    Origin lived on four fixtures and every cross-book board read one book)."""
    from sportsdata_agents.operations.resolution.resolver import _side_ok, _tokens

    assert _side_ok(_tokens("NSW"), _tokens("New South Wales"))
    assert _side_ok(_tokens("QLD"), _tokens("Queensland Maroons"))
    assert not _side_ok(_tokens("NSW"), _tokens("North Sydney"))  # 2 words ≠ 3 initials
    assert not _side_ok(_tokens("GWS"), _tokens("Gold Coast Suns"))  # initials differ
    # Unibet marks women's sides "(W)"; TAB abbreviates tennis given names
    assert _side_ok(_tokens("Cronulla Sharks (W)"), _tokens("Cronulla Sharks Women"))
    assert not _side_ok(_tokens("Cronulla Sharks (W)"), _tokens("Cronulla Sharks"))
    assert _side_ok(_tokens("Arango E"), _tokens("Emiliana Arango"))
    assert not _side_ok(_tokens("Arango E"), _tokens("Bianca Arango"))
