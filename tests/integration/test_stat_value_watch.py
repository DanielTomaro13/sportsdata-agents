"""stat_value watch: prop-ladder inconsistency alerts from Dabble stat lines."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import OddsSnapshot, Subscription
from sportsdata_agents.operations.monitoring import run_watches

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 7, 6, 9, 0, tzinfo=dt.UTC)


def _prop(line: float, side: str, odds: float) -> OddsSnapshot:
    return OddsSnapshot(
        captured_at=NOW - dt.timedelta(minutes=15), provider="dabble", book="Dabble",
        sport="baseball", event_external_id="MLB-1", event_name="Red Sox @ Angels",
        market=f"pitcher strikeouts o/u ({line})", selection=side, odds=odds,
        meta={"player": "Ace Pitcher", "stat": "strikeouts", "stat_line": line,
              "line_type": side},
    )


class StubEngine:
    """Engine whose fit says the 8.5-over rung pays far above the ladder's level."""

    def sports(self) -> list[str]:
        return ["baseball"]

    def stat_prices(self, entity: str, stat: str, quotes: list[dict[str, Any]],
                    thresholds: list[int] | None = None) -> dict[str, Any]:
        assert entity == "Ace Pitcher" and stat == "strikeouts"
        assert any(q.get("devigged") for q in quotes)  # O/U pairs pin the level
        return {"entity": entity, "stat": stat,
                "model": {"mu": 6.2, "dispersion": 1.35},
                "fit": {"margin": 1.05, "rmse_log": 0.02, "n_quotes": len(quotes)},
                "prices": [
                    {"market": "stat_threshold:strikeouts", "selection": entity,
                     "line": 6.0, "fair_probability": 0.52},
                    {"market": "stat_threshold:strikeouts", "selection": entity,
                     "line": 7.0, "fair_probability": 0.36},
                    {"market": "stat_threshold:strikeouts", "selection": entity,
                     "line": 9.0, "fair_probability": 0.12},
                ]}


async def test_stat_value_fires_on_the_inconsistent_rung(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with db_sessionmaker() as s:
        for row in (
            _prop(5.5, "over", 1.85), _prop(5.5, "under", 1.95),
            _prop(6.5, "over", 2.60), _prop(6.5, "under", 1.50),
            # fair for over 8.5 is 0.12 -> fair odds 8.33; the book pays 12.0: +44%
            _prop(8.5, "over", 12.0),
        ):
            s.add(row)
        s.add(Subscription(tenant_id="t", workspace_id="w", name="props",
                           kind="stat_value", channel="log",
                           params={"min_edge_pct": 5.0, "min_rungs": 3}))
        await s.commit()

    pushed: list[str] = []

    async def pusher(sub: Subscription, message: str) -> bool:
        pushed.append(message)
        return True

    import sportsdata_agents.quant.engines as engines_module

    # bare platform -> the watch skips cleanly
    monkeypatch.setattr(engines_module, "resolve_engine", lambda: None)
    report = await run_watches(db_sessionmaker, pusher=pusher, now=NOW)
    assert report["alerts"] == 0

    monkeypatch.setattr(engines_module, "resolve_engine", lambda: StubEngine())
    report = await run_watches(db_sessionmaker, pusher=pusher, now=NOW)
    assert report["alerts"] == 1, pushed
    assert "stat value" in pushed[0] and "Ace Pitcher" in pushed[0] and "12.00" in pushed[0]

    # unchanged ladder -> deduped
    report = await run_watches(db_sessionmaker, pusher=pusher, now=NOW + dt.timedelta(minutes=5))
    assert report["alerts"] == 0
