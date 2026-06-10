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
    overrides.write_text('{"markets": {"h2h": ["match odds"]}, "sports": {"soccer": ["epl"]}}')
    monkeypatch.setenv("SPORTSDATA_AGENTS_DICTIONARY_OVERRIDES", str(overrides))
    reload_dictionary()
    try:
        assert canonical_market("Match Odds") == "h2h"
        assert canonical_sport("EPL") == "soccer"
    finally:
        monkeypatch.delenv("SPORTSDATA_AGENTS_DICTIONARY_OVERRIDES")
        reload_dictionary()
    assert canonical_market("match odds") == "match odds"  # gone with the override file


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
    # US '@' lists away first — normalised to (home, away)
    assert split_sides("San Antonio Spurs @ New York Knicks") == ("New York Knicks", "San Antonio Spurs")
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
