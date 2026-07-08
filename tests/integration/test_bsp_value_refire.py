"""Regression: form-value (bsp_value / racing_value) alerts re-fire only when the
edge GROWS, never when a price firms in.

Lived bug (v0.79.16): the dedupe key bands by edge, so a shortening price crossed
a band DOWNWARD and re-alerted the same runner at a worse price/smaller edge.
The guard now skips unless the new band is strictly higher than the last alert.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import (
    Alert,
    ModelArtifact,
    OddsSnapshot,
    Prediction,
    RaceForm,
    Subscription,
)
from sportsdata_agents.operations.monitoring import run_watches

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 7, 8, 4, 0, tzinfo=dt.UTC)
JUMP = NOW + dt.timedelta(minutes=25)
RACE_KEY = "R:BAT:1:2026-07-08"


async def _pusher(sub: Subscription, message: str) -> bool:
    return True


async def _betfair(sf: async_sessionmaker[AsyncSession], name: str, number: int,
                   back: float, *, captured_offset_min: int) -> None:
    async with sf() as s:
        s.add(OddsSnapshot(
            captured_at=NOW - dt.timedelta(minutes=captured_offset_min),
            provider="betfair", book="Betfair", sport="horse_racing",
            event_external_id="BF", event_name="Bathurst (AUS) 8th Jul",
            market="win", selection=name.lower(), odds=back, start_time=JUMP,
            meta={"runner": name, "runner_number": number, "total_matched": 25_000.0,
                  "race": "R1 1400m Hcap", "market_id": "1.1"},
        ))
        await s.commit()


async def _seed(sf: async_sessionmaker[AsyncSession]) -> None:
    async with sf() as s:
        s.add(RaceForm(
            race_key=RACE_KEY, meeting_date="2026-07-08", race_type="R",
            venue_mnemonic="BAT", race_number=1, start_time=JUMP,
            runners=[{"number": 1, "name": "Alpha"}, {"number": 2, "name": "Beta"}],
            captured_at=NOW - dt.timedelta(minutes=2),
        ))
        model = ModelArtifact(tenant_id="t", workspace_id="w",
                              name="engine-form:racing", sport="horse_racing")
        s.add(model)
        await s.flush()
        # Alpha fair 6.67 (the value runner), Beta fair ~2.0 (the favourite)
        for sel, prob in (("1", "0.15"), ("2", "0.50")):
            s.add(Prediction(tenant_id="t", workspace_id="w", model_id=model.id,
                             event_external_id=RACE_KEY, market="win", selection=sel,
                             prob=Decimal(prob), predicted_at=NOW - dt.timedelta(minutes=1)))
        s.add(Subscription(tenant_id="t", workspace_id="w", name="bsp",
                           kind="bsp_value", params={}, channel="log"))
        await s.commit()
    # Beta priced short (no edge); Alpha priced long enough to be value
    await _betfair(sf, "Beta", 2, 2.05, captured_offset_min=2)


def _edge(prob: float, back: float, commission: float = 0.05) -> float:
    effective = 1.0 + (back - 1.0) * (1.0 - commission)
    return (effective * prob - 1.0) * 100.0


async def test_bsp_value_refires_only_on_a_growing_edge(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    sf = db_sessionmaker
    await _seed(sf)

    # sanity on the fixture: the three prices sit in distinct edge bands
    assert int(_edge(0.15, 9.2) / 5) == 6      # round 1 — fires
    assert int(_edge(0.15, 8.16) / 5) == 3     # round 2 — firmer, LOWER band
    assert int(_edge(0.15, 9.9) / 5) == 8      # round 3 — drifted out, HIGHER band

    # round 1: Alpha is value at 9.2 (+~32%, band 6) → one alert
    await _betfair(sf, "Alpha", 1, 9.2, captured_offset_min=2)
    r1 = await run_watches(sf, pusher=_pusher, now=NOW)
    assert r1["alerts"] == 1
    async with sf() as s:
        keys = [k for (k,) in (await s.execute(
            select(Alert.dedupe_key).where(Alert.kind == "bsp_value"))).all()]
    assert keys == [f"bsp_value:{RACE_KEY}:6"]

    # round 2: price FIRMS to 8.16 (+~17%, band 3) — edge shrank. Must NOT re-fire.
    await _betfair(sf, "Alpha", 1, 8.16, captured_offset_min=1)
    r2 = await run_watches(sf, pusher=_pusher, now=NOW)
    assert r2["alerts"] == 0, "a firming price (lower band) must not re-alert"

    # round 3: price DRIFTS out to 9.9 (+~42%, band 8) — edge grew past band 6.
    await _betfair(sf, "Alpha", 1, 9.9, captured_offset_min=0)
    r3 = await run_watches(sf, pusher=_pusher, now=NOW)
    assert r3["alerts"] == 1, "a materially bigger edge (higher band) should re-alert"
    async with sf() as s:
        bands = sorted(k.rsplit(":", 1)[-1] for (k,) in (await s.execute(
            select(Alert.dedupe_key).where(Alert.kind == "bsp_value"))).all())
    assert bands == ["6", "8"]
