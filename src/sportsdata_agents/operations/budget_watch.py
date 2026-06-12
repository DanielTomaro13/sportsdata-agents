"""Budget-breach push — tell the operator when spend crosses the cap.

The model gateway already *enforces* the budget (refuses calls once it's spent —
see :mod:`budget_guard`); this is the notification half: a small operator-only
conductor job that checks the period budget and pushes to Slack/Discord on a
breach, rate-limited so a breached period alerts once, not every tick.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

_STATE_KEY = "budget_breach_pushed_at"
_MIN_INTERVAL_H = 12.0  # re-alert at most twice a day while a period stays breached


async def push_budget_breach(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: dt.datetime | None = None,
    min_interval_h: float = _MIN_INTERVAL_H,
) -> dict[str, Any]:
    """Push an alert if the period budget is breached and we haven't recently.

    Returns a small dict describing what happened (for the CLI/log). Never raises
    on a missing target or warehouse hiccup — it's a best-effort notification."""
    from sportsdata_agents.observability.notify import operator_broadcast
    from sportsdata_agents.operations import costs
    from sportsdata_agents.tools.ops import read_ops_state, write_ops_state

    now = now or dt.datetime.now(dt.UTC)
    try:
        status = await costs.budget_status(session_factory, now=now)
    except Exception as e:
        logger.warning("budget watch: could not read budget status (%s)", e)
        return {"pushed": False, "reason": f"status unavailable: {e}"}

    if not status:
        return {"pushed": False, "reason": "no budget set"}
    if not status["breached"]:
        return {"pushed": False, "reason": "within budget", "pct": status["pct"]}

    state = read_ops_state()
    last_raw = state.get(_STATE_KEY)
    if last_raw:
        try:
            last = dt.datetime.fromisoformat(last_raw)
            if (now - last).total_seconds() < min_interval_h * 3600:
                return {"pushed": False, "reason": "rate-limited", "pct": status["pct"]}
        except ValueError:
            pass  # corrupt timestamp → treat as never pushed

    msg = (
        f":money_with_wings: budget breach — spent ${status['spent_usd']:.2f} of "
        f"${status['cap_usd']:.2f} this {status['period']} ({status['pct']:.0f}%). "
        f"Model calls are now refused until the period rolls over."
    )
    results = await operator_broadcast(msg)
    state[_STATE_KEY] = now.isoformat()
    write_ops_state(state)
    logger.warning("budget breach pushed: %s", msg)
    return {"pushed": any(results.values()) if results else False, "targets": results,
            "pct": status["pct"]}
