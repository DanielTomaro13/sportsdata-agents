"""PeriodBudgetGuard: the cross-run daily/weekly/monthly budget, enforced at the
model-call chokepoint. The DB read is stubbed — this exercises the ceiling logic
(baseline + in-process accrual, refusal, and window rollover) in isolation."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from sportsdata_agents.models.gateway import BudgetExceededError
from sportsdata_agents.operations import budget_guard as bg
from sportsdata_agents.operations import costs

pytestmark = pytest.mark.unit


def _set_budget(monkeypatch: pytest.MonkeyPatch, budget: dict[str, Any] | None) -> None:
    monkeypatch.setattr(costs, "get_budget", lambda: budget)


def _set_baseline(monkeypatch: pytest.MonkeyPatch, *values: float) -> None:
    """Stub the warehouse read: return each value in turn, clamping at the last."""
    seq = list(values) or [0.0]
    state = {"i": 0}

    async def fake_spend(_sf: Any, _period: str, *, now: Any = None) -> float:
        i = min(state["i"], len(seq) - 1)
        state["i"] += 1
        return seq[i]

    monkeypatch.setattr(costs, "period_spend", fake_spend)


async def test_no_budget_set_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_budget(monkeypatch, None)
    await bg.PeriodBudgetGuard(session_factory=None).precheck()  # no DB, no raise


async def test_zero_cap_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_budget(monkeypatch, {"period": "monthly", "cap_usd": 0})
    await bg.PeriodBudgetGuard(session_factory=None).precheck()


async def test_committed_baseline_over_cap_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_budget(monkeypatch, {"period": "monthly", "cap_usd": 5.0})
    _set_baseline(monkeypatch, 5.0)  # already spent the cap before this process
    with pytest.raises(BudgetExceededError):
        await bg.PeriodBudgetGuard(session_factory=object()).precheck()


async def test_in_process_accrual_trips_within_a_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_budget(monkeypatch, {"period": "daily", "cap_usd": 0.05})
    _set_baseline(monkeypatch, 0.0)
    guard = bg.PeriodBudgetGuard(session_factory=object())
    await guard.precheck()  # 0.00 < 0.05 → ok
    guard.charge(0.03)
    await guard.precheck()  # 0.03 < 0.05 → ok
    guard.charge(0.03)  # running total 0.06 ≥ 0.05
    with pytest.raises(BudgetExceededError):
        await guard.precheck()


async def test_window_rollover_rebaselines_and_resets(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the period rolls over, the guard re-reads committed spend and drops its
    in-process tally — a fresh window starts clean even if the last one was maxed."""
    _set_budget(monkeypatch, {"period": "daily", "cap_usd": 1.0})
    _set_baseline(monkeypatch, 0.9, 0.0)  # window 1 baseline 0.9; window 2 baseline 0.0
    window = {"start": dt.datetime(2026, 6, 13, tzinfo=dt.UTC)}
    monkeypatch.setattr(costs, "period_start", lambda period, now=None: window["start"])

    guard = bg.PeriodBudgetGuard(session_factory=object())
    await guard.precheck()  # baseline 0.9 < 1.0 → ok
    guard.charge(0.05)
    await guard.precheck()  # 0.95 < 1.0 → ok (tally persists within the window)

    window["start"] = dt.datetime(2026, 6, 14, tzinfo=dt.UTC)  # next day
    await guard.precheck()  # re-baselined to 0.0, tally reset → well under the cap
    guard.charge(0.05)
    await guard.precheck()  # 0.05 < 1.0 → still ok
