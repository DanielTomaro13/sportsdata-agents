"""Model-spend reporting + a budget (the operator's cost console).

Every model call is already metered into ``agent_runs`` (cost, tokens, model, tier,
agent, plane). This rolls those rows up — by day, agent, model, and ops-vs-product —
so the operator can see where the money goes, and checks it against a budget the
operator sets. The budget is a small local config (``<data_dir>/budget.json``); the
spend itself comes from the warehouse.

Ops spend is tenant ``platform`` (the operator's own maintenance); everything else is
product spend (the user's questions). Keeping them separate is the whole point — you
want to know what the platform costs YOU vs what serving requests costs.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import AgentRun

_OPS_TENANT = "platform"


def _budget_path() -> Any:
    from sportsdata_agents.paths import data_dir

    return data_dir() / "budget.json"


def get_budget() -> dict[str, Any] | None:
    """The operator's budget {period, cap_usd} or None if unset."""
    path = _budget_path()
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None
    return None


def set_budget(cap_usd: float, period: str = "monthly") -> dict[str, Any]:
    """Persist the budget. period ∈ {daily, weekly, monthly}."""
    if period not in ("daily", "weekly", "monthly"):
        raise ValueError("period must be daily, weekly or monthly")
    if cap_usd < 0:
        raise ValueError("cap must be >= 0")
    budget = {"period": period, "cap_usd": round(float(cap_usd), 2)}
    _budget_path().write_text(json.dumps(budget), encoding="utf-8")
    return budget


def _period_start(period: str, now: dt.datetime) -> dt.datetime:
    if period == "daily":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "weekly":
        monday = now - dt.timedelta(days=now.weekday())
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)  # monthly


def period_start(period: str, now: dt.datetime | None = None) -> dt.datetime:
    """Start of the budget window containing ``now`` (public; the guard tracks
    rollovers by watching this value change)."""
    return _period_start(period, now or dt.datetime.now(dt.UTC))


async def spend_report(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    days: int = 7,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Roll up agent-run spend over the last ``days``."""
    now = now or dt.datetime.now(dt.UTC)
    cutoff = now - dt.timedelta(days=days)
    async with session_factory() as session:
        rows = (
            await session.execute(select(AgentRun).where(AgentRun.created_at >= cutoff))
        ).scalars().all()

    total = 0.0
    ops_total = 0.0
    by_day: dict[str, float] = {}
    by_agent: dict[str, dict[str, Any]] = {}
    by_model: dict[str, float] = {}
    for r in rows:
        cost = float(r.cost_usd or 0)
        total += cost
        if r.tenant_id == _OPS_TENANT:
            ops_total += cost
        day = r.created_at.date().isoformat() if r.created_at else "?"
        by_day[day] = round(by_day.get(day, 0.0) + cost, 6)
        agent = by_agent.setdefault(r.agent, {"cost": 0.0, "runs": 0, "errors": 0})
        agent["cost"] = round(agent["cost"] + cost, 6)
        agent["runs"] += 1
        agent["errors"] += 1 if r.status == "error" else 0
        if r.model:
            by_model[r.model] = round(by_model.get(r.model, 0.0) + cost, 6)

    return {
        "days": days,
        "runs": len(rows),
        "total_usd": round(total, 4),
        "ops_usd": round(ops_total, 4),
        "product_usd": round(total - ops_total, 4),
        "by_day": dict(sorted(by_day.items())),
        "by_agent": dict(sorted(by_agent.items(), key=lambda kv: -kv[1]["cost"])),
        "by_model": dict(sorted(by_model.items(), key=lambda kv: -kv[1])),
    }


async def period_spend(
    session_factory: async_sessionmaker[AsyncSession], period: str, *, now: dt.datetime | None = None
) -> float:
    """Total spend since the start of the current budget period."""
    now = now or dt.datetime.now(dt.UTC)
    start = _period_start(period, now)
    async with session_factory() as session:
        rows = (
            await session.execute(select(AgentRun.cost_usd).where(AgentRun.created_at >= start))
        ).scalars().all()
    return round(sum(float(c or 0) for c in rows), 4)


async def budget_status(
    session_factory: async_sessionmaker[AsyncSession], *, now: dt.datetime | None = None
) -> dict[str, Any] | None:
    """How the current period's spend tracks against the budget, or None if unset."""
    budget = get_budget()
    if not budget:
        return None
    spent = await period_spend(session_factory, budget["period"], now=now)
    cap = float(budget["cap_usd"])
    return {
        "period": budget["period"],
        "cap_usd": cap,
        "spent_usd": spent,
        "pct": round(spent / cap * 100, 1) if cap > 0 else 0.0,
        "breached": cap > 0 and spent > cap,
    }
