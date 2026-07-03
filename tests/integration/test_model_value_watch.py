"""model_value watch: engine-vs-book consistency alerts through run_watches."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import Alert, Price, Subscription
from sportsdata_agents.operations import monitoring
from sportsdata_agents.operations.monitoring import (
    _footy_engine_inputs,
    _split_selection,
    run_watches,
)
from sportsdata_agents.quant.engines import EnginePrice

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 7, 3, 9, 0, tzinfo=dt.UTC)


def _price(market: str, selection: str, odds: float, minutes_ago: int = 5) -> Price:
    return Price(
        changed_at=NOW - dt.timedelta(minutes=minutes_ago),
        provider="sportsbet", book="sportsbet", sport="afl",
        event_external_id="AFL-1", market=market, selection=selection, odds=odds,
    )


class StubEngine:
    """Engine that thinks the +18.5 away line is badly priced by the book."""

    def sports(self) -> list[str]:
        return ["afl"]

    def price_board(self, sport: str, fixture_id: str, quotes: dict[str, Any]) -> list[EnginePrice]:
        assert quotes["h2h"] == [1.44, 2.81]
        assert quotes["total"] == [186.5, 1.9, 1.9]
        return [
            EnginePrice("h2h", "home", 0.66, std_error=0.003),
            EnginePrice("line", "away", 0.62, line=18.5, std_error=0.003),  # book pays 1.90: +17.8% edge
            EnginePrice("total", "over", 0.51, line=186.5, std_error=0.02),  # inside noise band
        ]


def test_selection_parsing() -> None:
    assert _split_selection("home") == ("home", None)
    assert _split_selection("away +18.5") == ("away", 18.5)
    assert _split_selection("over 220.5") == ("over", 220.5)
    assert _split_selection("Gossamer Glow") == ("Gossamer Glow", None)


def test_footy_inputs_need_full_anchors() -> None:
    seed, quotes = _footy_engine_inputs([_price("2way", "home", 1.44)])
    assert seed is None and quotes == []


async def test_model_value_watch_fires_noise_gates_and_degrades(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with db_sessionmaker() as s:
        for row in (
            _price("2way", "home", 1.44),
            _price("2way", "away", 2.81),
            _price("total", "over 186.5", 1.90),
            _price("total", "under 186.5", 1.90),
            _price("spread", "away +18.5", 1.90),
            _price("total", "over 200.5", 3.00, minutes_ago=90),  # stale: outside max_age
        ):
            s.add(row)
        s.add(Subscription(tenant_id="t", workspace_id="w", name="afl-model",
                           kind="model_value", channel="log",
                           params={"sport": "afl", "min_edge_pct": 3.0}))
        await s.commit()

    pushed: list[str] = []

    async def pusher(sub: Subscription, message: str) -> bool:
        pushed.append(message)
        return True

    # no engine configured -> the watch skips cleanly, nothing fires
    monkeypatch.setattr(monitoring, "run_watches", run_watches)
    import sportsdata_agents.quant.engines as engines_module

    monkeypatch.setattr(engines_module, "resolve_engine", lambda: None)
    report = await run_watches(db_sessionmaker, pusher=pusher, now=NOW)
    assert report["alerts"] == 0 and pushed == []

    # engine present -> exactly the out-of-band derivative fires
    monkeypatch.setattr(engines_module, "resolve_engine", lambda: StubEngine())
    report = await run_watches(db_sessionmaker, pusher=pusher, now=NOW)
    assert report["alerts"] == 1, report
    assert "model value" in pushed[0] and "+17.8%" in pushed[0]

    async with db_sessionmaker() as s:
        alerts = (await s.execute(Alert.__table__.select())).all()
    assert len(alerts) == 1  # h2h anchor ~0 edge; total over noise-gated; stale row ignored

    # same condition again -> deduped, no double fire
    report = await run_watches(db_sessionmaker, pusher=pusher, now=NOW + dt.timedelta(minutes=5))
    assert report["alerts"] == 0
