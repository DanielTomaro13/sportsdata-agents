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


def test_the_engine_never_displaces_betfair_even_covering_the_whole_field():
    """THE SAFETY PROPERTY, reversed deliberately in 764d2da.

    The engine used to claim the field at 60% coverage and let Betfair fill the rest.
    That was harmless only because race_form was empty so the branch never ran — and
    form ingestion was about to arm it under a bot betting real money.

    Betfair's calibration on this data is measured (AU thoroughbreds, ratio 1.004,
    log-loss 0.2879 over 2,305 graded runners). The engine's is not measured at all. An
    unmeasured model does not get to displace a measured one, so where the exchange
    prices a runner it wins — however completely the engine covers the race.

    This test asserted the OLD ordering and was left behind by that commit, which is why
    it failed for four commits: it was demanding precisely the behaviour the change
    existed to remove.
    """
    runners = [
        _runner(1, tote=0.20, bf=4.0, engine=0.50, corp=3.0),
        _runner(2, tote=0.30, bf=3.0, engine=0.25, corp=5.0),
        _runner(3, tote=0.25, bf=4.0, engine=0.15, corp=8.0),
        _runner(4, tote=0.25, bf=5.0, engine=0.10, corp=12.0),
    ]
    finalize_snapshot(RaceSnapshot(ts=0.0, runners=runners))

    assert all(r.fair_source == "betfair" for r in runners), (
        "the exchange prices every runner here, so the engine must not claim any of them"
    )
    # The engine's 0.50 would have made runner 1 a 2.0 shot. The de-vigged exchange makes
    # it 4.13: mids are 4/3/4/5, so Σ(1/odds) = 1.033 and runner 1's fair prob is
    # 0.25/1.033 = 0.242 — slightly LONGER than the raw 4.0 mid, because removing the
    # overround gives probability back to the field. Pinning the number, not just the
    # source, is what catches the ordering quietly flipping back.
    assert runners[0].fair_price == pytest.approx(4.13, abs=0.01)
    # ...and the value edge follows the measured source, not the unmeasured one:
    # corp 3.0 x 0.242 - 1 = -27.4%, the opposite sign to the engine's +50%. A board
    # that took the engine's price would show this runner as value; it is not.
    assert runners[0].value_pct == pytest.approx(-27.4, abs=0.1)

    # The engine probability is still CARRIED — the board shows it, it just does not
    # price. That is the whole point of the reversal: informative, not authoritative.
    assert runners[0].engine_prob == 0.50


def test_the_engine_fills_only_runners_the_exchange_does_not_price():
    """The engine's remaining job: a runner Betfair has no market for still gets a fair
    price, renormalised over the engine's own covered set. The placer refuses those
    anyway (require_betfair_fair), so this informs the board without reaching a bet."""
    runners = [
        _runner(1, tote=0.20, bf=4.0, engine=0.50, corp=3.0),
        _runner(2, tote=0.30, bf=3.0, engine=0.25, corp=5.0),
        _runner(3, tote=0.25, bf=4.0, engine=0.15, corp=8.0),
        _runner(4, tote=0.25, bf=5.0, engine=0.10, corp=12.0),
        _runner(5, tote=0.05, engine=0.05, corp=30.0),   # no exchange market
    ]
    finalize_snapshot(RaceSnapshot(ts=0.0, runners=runners))

    assert [r.fair_source for r in runners[:4]] == ["betfair"] * 4
    assert runners[4].fair_source == "engine", (
        "a runner the exchange does not price is exactly what the engine is still for"
    )


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
