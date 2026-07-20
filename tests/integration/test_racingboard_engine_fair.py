"""The racing board's engine fair source: reads engine-form:racing win probs
from the warehouse via the exact key bridge, and finalize_snapshot prefers the
engine over market de-vig when it covers the field — degrading cleanly to
Betfair/tote when it doesn't."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import ModelArtifact, Prediction
from sportsdata_agents.interfaces.racingboard.engine_fair import (
    _agents_key,
    engine_prices,
)
from sportsdata_agents.interfaces.racingboard.models import RaceSnapshot, RunnerFlow
from sportsdata_agents.interfaces.racingboard.sources import finalize_snapshot

pytestmark = pytest.mark.integration


def test_key_bridge_reorders_board_key_to_the_tab_key():
    # board keys {code}:{venue}:{no}:{date}; warehouse keys {date}:{code}:{venue}:{no}
    assert _agents_key("2026-07-21", "R", "BAT", 4) == "2026-07-21:R:BAT:4"


async def _seed(s: AsyncSession, key: str, probs: dict[int, float]) -> None:
    model = ModelArtifact(tenant_id="t", workspace_id="w",
                          name="engine-form:racing", version=1,
                          sport="horse_racing", params={}, calibration={})
    s.add(model)
    await s.flush()
    for number, prob in probs.items():
        s.add(Prediction(tenant_id="t", workspace_id="w", model_id=model.id,
                         provider="tab", event_external_id=key, market="win",
                         selection=str(number), prob=prob,
                         predicted_at=dt.datetime(2026, 7, 21, tzinfo=dt.UTC)))
    await s.commit()


async def test_engine_prices_reads_predictions_by_bridged_key(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    key = _agents_key("2026-07-21", "R", "BAT", 4)
    async with db_sessionmaker() as s:
        await _seed(s, key, {1: 0.40, 2: 0.35, 3: 0.25})

    probs = await engine_prices(date="2026-07-21", code="R", venue_mnem="BAT",
                                race_no=4, session_factory=db_sessionmaker)
    assert probs == {1: 0.4, 2: 0.35, 3: 0.25}

    # a race the engine has nothing for -> empty, not an error
    none = await engine_prices(date="2026-07-21", code="R", venue_mnem="SAN",
                               race_no=9, session_factory=db_sessionmaker)
    assert none == {}


def _runner(number: int, *, tote: float, bf: float | None = None,
            engine: float | None = None, corp: float | None = None) -> RunnerFlow:
    r = RunnerFlow(number=number, name=f"r{number}", tote_pool_share=tote,
                   engine_prob=engine, corp_best=corp)
    if bf is not None:
        r.bf_back, r.bf_lay = bf * 0.99, bf * 1.01
    return r


def test_finalize_prefers_the_engine_when_it_covers_the_field():
    runners = [
        _runner(1, tote=0.20, bf=4.0, engine=0.50, corp=3.0),
        _runner(2, tote=0.30, bf=3.0, engine=0.25, corp=5.0),
        _runner(3, tote=0.25, bf=4.0, engine=0.15, corp=8.0),
        _runner(4, tote=0.25, bf=5.0, engine=0.10, corp=12.0),
    ]
    finalize_snapshot(RaceSnapshot(ts=0.0, runners=runners))
    assert all(r.fair_source == "engine" for r in runners)
    # runner 1: engine 0.50 (normalised over 1.0) -> fair 2.0, not the ~4.0 mid
    assert runners[0].fair_price == 2.0
    # value uses the engine fair: corp 3.0 * 0.50 - 1 = +50%
    assert runners[0].value_pct == 50.0


def test_finalize_falls_back_to_betfair_when_engine_is_absent():
    runners = [
        _runner(1, tote=0.20, bf=4.0, corp=3.0),
        _runner(2, tote=0.30, bf=3.0),
        _runner(3, tote=0.25, bf=4.0),
        _runner(4, tote=0.25, bf=5.0),
    ]
    finalize_snapshot(RaceSnapshot(ts=0.0, runners=runners))
    assert all(r.fair_source == "betfair" for r in runners)
    assert runners[0].engine_prob is None


def test_finalize_partial_engine_coverage_blends_sources():
    # 6-runner field (threshold 4): only 2 have engine (below floor -> skip),
    # 5 have Betfair (>=4 -> Betfair leads), the last has tote only (filled).
    runners = [
        _runner(1, tote=0.20, bf=4.0, engine=0.50),
        _runner(2, tote=0.20, bf=3.0, engine=0.30),
        _runner(3, tote=0.15, bf=4.0),
        _runner(4, tote=0.15, bf=5.0),
        _runner(5, tote=0.15, bf=6.0),
        _runner(6, tote=0.15),  # no bf -> tote fills it
    ]
    finalize_snapshot(RaceSnapshot(ts=0.0, runners=runners))
    assert runners[0].fair_source == "betfair"   # engine below floor, bf leads
    assert runners[5].fair_source == "tote"      # bf-blind -> tote fill
    assert not any(r.fair_source == "engine" for r in runners)
