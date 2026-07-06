"""price-slate recorder: engine boards persisted as predictions (value-loop measurement)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import ModelArtifact, Prediction, Price
from sportsdata_agents.data.repository import TenantScope
from sportsdata_agents.quant.engines import EnginePrice
from sportsdata_agents.quant.slate import record_slate

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 7, 6, 9, 0, tzinfo=dt.UTC)
SCOPE = TenantScope("t", "w")


def _price(market: str, selection: str, odds: float, minutes_ago: int = 5) -> Price:
    return Price(
        changed_at=NOW - dt.timedelta(minutes=minutes_ago),
        provider="sportsbet", book="sportsbet", sport="afl",
        event_external_id="AFL-9", market=market, selection=selection, odds=odds,
    )


class StubEngine:
    def sports(self) -> list[str]:
        return ["afl"]

    def price_board(self, sport: str, fixture_id: str, quotes: dict[str, Any]) -> list[EnginePrice]:
        assert sport == "afl" and quotes["h2h"] == [1.5, 2.6]
        return [
            EnginePrice("h2h", "home", 0.64, std_error=0.003),
            EnginePrice("line", "away", 0.55, line=12.5, std_error=0.003),
            EnginePrice("total", "over", 0.52, line=170.5, std_error=0.004),
            EnginePrice("stat_h2h:disposals", "A beats B", 0.5),  # no warehouse convention -> skipped
            EnginePrice("h2h_3way", "draw", 0.0, std_error=0.0),  # degenerate corner -> skipped
        ]


async def test_slate_records_once_and_dedupes(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with db_sessionmaker() as s:
        for row in (
            _price("2way", "home", 1.5),
            _price("2way", "away", 2.6),
            _price("total", "over 170.5", 1.9),
            _price("total", "under 170.5", 1.9),
        ):
            s.add(row)
        await s.commit()

    import sportsdata_agents.quant.engines as engines_module

    # no engine -> honest degradation, nothing recorded
    monkeypatch.setattr(engines_module, "resolve_engine", lambda: None)
    async with db_sessionmaker() as s:
        report = await record_slate(s, SCOPE, now=NOW, sports=(("afl", "afl"),))
    assert report["recorded"] == 0 and "error" in report

    monkeypatch.setattr(engines_module, "resolve_engine", lambda: StubEngine())
    async with db_sessionmaker() as s:
        report = await record_slate(s, SCOPE, now=NOW, sports=(("afl", "afl"),))
    assert report == {"recorded": 3, "events": 1, "skipped_dedupe": 0, "skipped_unseedable": 0}

    async with db_sessionmaker() as s:
        artifact = (await s.execute(select(ModelArtifact))).scalars().one()
        assert artifact.name == "engine:afl" and artifact.params["source"] == "price-slate"
        rows = (await s.execute(select(Prediction))).scalars().all()
        by_key = {(p.market, p.selection): float(p.prob) for p in rows}
    # engine families land under the WAREHOUSE conventions (provider=book)
    assert by_key[("h2h", "home")] == pytest.approx(0.64)
    assert by_key[("spread", "away 12.5")] == pytest.approx(0.55)
    assert by_key[("total", "over 170.5")] == pytest.approx(0.52)
    assert all(p.provider == "sportsbet" for p in rows)

    # a second run inside the dedupe window: anchors move again, but the
    # (book, event) already has a fresh snapshot -> deduped, nothing recorded
    hour_on = NOW + dt.timedelta(hours=1)
    async with db_sessionmaker() as s:
        for sel, odds in (("home", 1.48), ("away", 2.65)):
            s.add(Price(changed_at=hour_on - dt.timedelta(minutes=3), provider="sportsbet",
                        book="sportsbet", sport="afl", event_external_id="AFL-9",
                        market="2way", selection=sel, odds=odds))
        await s.commit()
    async with db_sessionmaker() as s:
        report = await record_slate(s, SCOPE, now=hour_on, sports=(("afl", "afl"),))
    assert report["recorded"] == 0 and report["skipped_dedupe"] == 1

    # outside the window (fresh anchors) it snapshots again
    later = NOW + dt.timedelta(hours=13)
    async with db_sessionmaker() as s:
        s.add(Price(changed_at=later - dt.timedelta(minutes=4), provider="sportsbet", book="sportsbet",
                    sport="afl", event_external_id="AFL-9", market="2way", selection="home", odds=1.5))
        s.add(Price(changed_at=later - dt.timedelta(minutes=4), provider="sportsbet", book="sportsbet",
                    sport="afl", event_external_id="AFL-9", market="2way", selection="away", odds=2.6))
        s.add(Price(changed_at=later - dt.timedelta(minutes=4), provider="sportsbet", book="sportsbet",
                    sport="afl", event_external_id="AFL-9", market="total", selection="over 170.5", odds=1.9))
        s.add(Price(changed_at=later - dt.timedelta(minutes=4), provider="sportsbet", book="sportsbet",
                    sport="afl", event_external_id="AFL-9", market="total", selection="under 170.5", odds=1.9))
        await s.commit()
    async with db_sessionmaker() as s:
        report = await record_slate(s, SCOPE, now=later, sports=(("afl", "afl"),))
    assert report["recorded"] == 3 and report["skipped_dedupe"] == 0
