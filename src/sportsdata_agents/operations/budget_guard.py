"""Cross-run budget enforcement — the hard stop behind ``agents costs --set-budget``.

The per-run ``RunBudget`` caps a *single* run; this caps *total* spend across every
run in a budget period (daily/weekly/monthly). It's the guard the model gateway
consults at the call chokepoint, so once the operator's budget is spent NO run — a
customer's question or the platform's own ops maintenance — can call a model until
the period rolls over. Combined with the per-run ceiling, total period spend can
overshoot the cap by at most one in-flight call (cents), not one run.

Accuracy: each precheck combines this process's (frozen window baseline + in-flight
tally) with a FRESH read of the committed period spend, taking the higher of the two
(``max``, never ``sum`` — the committed total already includes this process's billed
spend). The fresh read means two processes spending in the same window (a daemon plus a
concurrent CLI run) each see the other's *committed* spend promptly; the only unseen
slack is the other process's single in-flight call, so total overshoot is bounded to
roughly one in-flight call per live process — cents, on a single-user desktop install.
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
        """Best estimate of total spend in the current window.

        Two views, combined with ``max`` (never ``sum`` — the committed total already
        includes whatever of this process's spend has been billed, so adding them would
        double-count):
          • the frozen window-start baseline + this process's in-flight tally, and
          • a FRESH read of committed period spend across ALL processes.
        Re-reading the committed total every check (not only on rollover) means a second
        process's committed spend is reflected promptly, bounding multi-process overshoot
        to roughly one in-flight call per process — not one whole run per process.
        """
        now = dt.datetime.now(dt.UTC)
        window = costs.period_start(period, now)
        rolled = self._window != window  # first call, period change, or calendar rollover
        if rolled:
            self._window = window
            self._process_usd = 0.0
        try:
            committed = await costs.period_spend(self._sf, period, now=now)
            if rolled:
                self._baseline_usd = committed  # freeze this window's baseline
        except Exception:  # warehouse hiccup: don't wedge runs, fall back to the baseline
            logger.warning("budget guard: could not read period spend; using prior baseline")
            committed = self._baseline_usd
        return max(self._baseline_usd + self._process_usd, committed)
