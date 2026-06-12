"""Budget-breach push: alerts the operator on a breach, rate-limited. The budget
status, the broadcast, and the ops-state store are all stubbed — this exercises
the decide-and-rate-limit logic in isolation."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from sportsdata_agents.operations import budget_watch as bw

pytestmark = pytest.mark.unit


def _wire(monkeypatch: pytest.MonkeyPatch, status: dict[str, Any] | None) -> dict[str, Any]:
    """Stub budget_status + ops-state + the broadcast; return a record of pushes."""
    from sportsdata_agents.observability import notify
    from sportsdata_agents.operations import costs
    from sportsdata_agents.tools import ops

    state: dict[str, Any] = {}
    pushed: list[str] = []

    async def fake_status(_sf: Any, *, now: Any = None) -> dict[str, Any] | None:
        return status

    async def fake_broadcast(text: str) -> dict[str, bool]:
        pushed.append(text)
        return {"slack": True}

    monkeypatch.setattr(costs, "budget_status", fake_status)
    monkeypatch.setattr(notify, "operator_broadcast", fake_broadcast)
    monkeypatch.setattr(ops, "read_ops_state", lambda: dict(state))
    monkeypatch.setattr(ops, "write_ops_state", lambda s: state.update(s))
    return {"state": state, "pushed": pushed}


async def test_no_budget_no_push(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _wire(monkeypatch, None)
    res = await bw.push_budget_breach(session_factory=None)
    assert res["pushed"] is False and not rec["pushed"]


async def test_within_budget_no_push(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _wire(monkeypatch, {"period": "monthly", "cap_usd": 50, "spent_usd": 10,
                              "pct": 20.0, "breached": False})
    res = await bw.push_budget_breach(session_factory=None)
    assert res["pushed"] is False and not rec["pushed"]


async def test_breach_pushes_once_then_rate_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _wire(monkeypatch, {"period": "monthly", "cap_usd": 50, "spent_usd": 60,
                              "pct": 120.0, "breached": True})
    first = await bw.push_budget_breach(session_factory=None)
    assert first["pushed"] is True and len(rec["pushed"]) == 1
    assert "budget breach" in rec["pushed"][0]

    second = await bw.push_budget_breach(session_factory=None)  # immediately again
    assert second == {"pushed": False, "reason": "rate-limited", "pct": 120.0}
    assert len(rec["pushed"]) == 1  # still just the one


async def test_breach_realerts_after_the_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _wire(monkeypatch, {"period": "monthly", "cap_usd": 50, "spent_usd": 60,
                              "pct": 120.0, "breached": True})
    now = dt.datetime.now(dt.UTC)
    await bw.push_budget_breach(session_factory=None, now=now)
    later = now + dt.timedelta(hours=13)  # past the 12h floor
    res = await bw.push_budget_breach(session_factory=None, now=later)
    assert res["pushed"] is True and len(rec["pushed"]) == 2
