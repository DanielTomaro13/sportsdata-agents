"""Signal bench: IC of raw signals against the market residual, from real
warehouse shapes (prices change-points + snapshot runner numbers + results)."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import (
    EventResult,
    ModelArtifact,
    OddsSnapshot,
    Prediction,
    Price,
)
from sportsdata_agents.quant.signal_bench import (
    _pearson,
    format_signal_bench,
    signal_bench,
)

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 7, 6, 9, 0, tzinfo=dt.UTC)


def test_pearson_needs_a_real_sample():
    assert _pearson([1.0] * 5, [1.0] * 5) is None            # too small
    assert _pearson([1.0] * 20, list(range(20))) is None      # zero variance
    r, t = _pearson(list(range(20)), list(range(20)))
    assert r > 0.999 and t > 10


async def _seed_race(s: AsyncSession, *, event_id: str, start: dt.datetime,
                     runners: list[tuple[str, str, float, float]],
                     winner: str) -> None:
    """runners: (number, name, opening_odds, closing_odds)."""
    for number, name, opening, closing in runners:
        s.add(OddsSnapshot(
            provider="tab_racing", book="TAB", sport="horse_racing",
            event_external_id=event_id, event_name="Testville R1",
            market="win", selection=name, odds=closing,
            captured_at=start - dt.timedelta(minutes=3), start_time=start,
            meta={"runner_number": int(number)}))
        s.add(Price(provider="tab_racing", book="TAB", sport="horse_racing",
                    event_external_id=event_id, market="win", selection=name,
                    odds=opening, changed_at=start - dt.timedelta(minutes=90)))
        s.add(Price(provider="tab_racing", book="TAB", sport="horse_racing",
                    event_external_id=event_id, market="win", selection=name,
                    odds=closing, changed_at=start - dt.timedelta(minutes=20)))
    s.add(EventResult(provider="tab_racing", sport="horse_racing",
                      event_external_id=event_id, winning_selection=winner,
                      start_time=start, settled_at=start + dt.timedelta(minutes=5)))


async def test_bench_scores_steam_and_control_from_warehouse_rows(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        # ten 3-runner races; the firming runner (odds shortening 20%) wins
        # every time. steam_60m is (closing-opening)/opening — NEGATIVE when
        # firming — and firming runners over-perform here, so the IC must
        # come out negative. The SIGN is the test.
        for i in range(10):
            start = NOW - dt.timedelta(days=1, minutes=i)
            await _seed_race(
                s, event_id=f"RACE-{i}", start=start, winner="1",
                runners=[("1", f"steamer {i}", 5.0, 4.0),
                         ("2", f"drifter {i}", 3.0, 3.6),
                         ("3", f"flat {i}", 6.0, 6.0)])
        await s.commit()

    async with db_sessionmaker() as s:
        report = await signal_bench(s, days=14.0, now=NOW)

    assert report["races"] == 10
    steam = report["signals"]["steam_60m"]
    assert steam["n"] == 30
    assert steam["ic"] is not None and steam["ic"] < -0.3  # firming wins
    control = report["signals"]["market_implied"]
    assert control["n"] == 30 and control["ic"] is not None
    text = format_signal_bench(report)
    assert "steam_60m" in text and "10 settled races" in text


async def test_engine_gap_joins_predictions_by_saddle_number(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        model = ModelArtifact(tenant_id="t", workspace_id="w",
                              name="engine-form:racing", version=1, sport="horse_racing",
                              params={}, calibration={})
        s.add(model)
        await s.flush()
        for i in range(4):
            start = NOW - dt.timedelta(days=1, minutes=i)
            await _seed_race(
                s, event_id=f"R2-{i}", start=start, winner="1",
                runners=[("1", f"liked {i}", 4.0, 4.0),
                         ("2", f"unliked {i}", 4.0, 4.0),
                         ("3", f"third {i}", 4.0, 4.0)])
            for number, prob in (("1", 0.40), ("2", 0.15), ("3", 0.15)):
                s.add(Prediction(
                    tenant_id="t", workspace_id="w", model_id=model.id,
                    provider="tab_racing", event_external_id=f"R2-{i}",
                    market="win", selection=number, prob=prob,
                    predicted_at=NOW - dt.timedelta(days=1, hours=2)))
        await s.commit()

    async with db_sessionmaker() as s:
        report = await signal_bench(s, days=14.0, now=NOW)

    gap = report["signals"]["engine_gap"]
    assert gap["n"] == 12
    # the engine liked every winner at 4.0 (gap +0.6 vs -0.4): strong positive
    assert gap["ic"] is not None and gap["ic"] > 0.5
