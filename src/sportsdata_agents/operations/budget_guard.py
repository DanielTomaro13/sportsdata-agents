"""Cross-run budget enforcement — the hard stop behind ``agents costs --set-budget``.

The per-run ``RunBudget`` caps a *single* run; this caps *total* spend across every
run in a budget period (daily/weekly/monthly). It's the guard the model gateway
consults at the call chokepoint, so once the operator's budget is spent NO run — a
customer's question or the platform's own ops maintenance — can call a model until
the period rolls over. Combined with the per-run ceiling, total period spend can
overshoot the cap by at most one in-flight call (cents), not one run.

Accuracy without a database round-trip per call: take a baseline of already-committed
period spend once (everything billed before this guard started in the current window),
then accumulate everything THIS process charges. Re-baseline only when the window rolls
over — at which point this process's running tally resets to zero too, so there's no
double-count. The one soft edge is two processes spending in the same window at the
same instant (a daemon plus a concurrent CLI run): each sees the other's *committed*
spend but not its in-flight spend, so the cap can be exceeded by at most one run's
per-run ceiling. For a single-user desktop install that is negligible and bounded.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.models.gateway import BudgetExceededError
from sportsdata_agents.operations import costs

logger = logging.getLogger(__name__)


class PeriodBudgetGuard:
    """A ``SpendGuard`` backed by the operator's period budget + the warehouse."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory
        self._window: dt.datetime | None = None  # start of the period we baselined
        self._baseline_usd = 0.0  # committed spend at that window's baseline
        self._process_usd = 0.0  # everything this process has charged since

    async def precheck(self) -> None:
        """Raise ``BudgetExceededError`` if the period cap is already spent."""
        budget = costs.get_budget()
        if not budget:
            return
        cap = float(budget.get("cap_usd", 0) or 0)
        if cap <= 0:
            return
        spent = await self._period_spent(str(budget["period"]))
        if spent >= cap:
            raise BudgetExceededError(round(spent, 4), cap)

    def charge(self, cost_usd: float) -> None:
        self._process_usd += max(cost_usd, 0.0)

    async def _period_spent(self, period: str) -> float:
        """Best estimate of total spend in the current window: a frozen DB baseline
        plus this process's running tally, re-baselined on a window rollover."""
        now = dt.datetime.now(dt.UTC)
        window = costs.period_start(period, now)
        if self._window != window:  # first call, period change, or calendar rollover
            try:
                self._baseline_usd = await costs.period_spend(self._sf, period, now=now)
            except Exception:  # warehouse hiccup: don't wedge runs, keep the prior baseline
                logger.warning("budget guard: could not read period spend; using prior baseline")
            self._window = window
            self._process_usd = 0.0
        return self._baseline_usd + self._process_usd
