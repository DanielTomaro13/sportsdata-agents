"""Cost rollup over agent_runs (operator console) — DB-backed."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import AgentRun
from sportsdata_agents.operations import costs

pytestmark = pytest.mark.integration


async def test_spend_report_rolls_up_and_splits_ops_vs_product(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    now = dt.datetime.now(dt.UTC)
    async with db_sessionmaker() as s:
        s.add(AgentRun(tenant_id="local", workspace_id="local", agent="odds_specialist",
                       model="anthropic/claude-sonnet-4-6", status="ok", cost_usd="0.05"))
        s.add(AgentRun(tenant_id="local", workspace_id="local", agent="odds_specialist",
                       model="anthropic/claude-sonnet-4-6", status="error", cost_usd="0.01"))
        s.add(AgentRun(tenant_id="platform", workspace_id="ops", agent="site_manager",
                       model="anthropic/claude-opus-4-8", status="ok", cost_usd="0.20"))
        await s.commit()

    report = await costs.spend_report(db_sessionmaker, days=7, now=now)
    assert report["runs"] == 3
    assert report["total_usd"] == pytest.approx(0.26)
    assert report["ops_usd"] == pytest.approx(0.20)         # platform tenant = ops spend
    assert report["product_usd"] == pytest.approx(0.06)
    assert report["by_agent"]["odds_specialist"]["runs"] == 2
    assert report["by_agent"]["odds_specialist"]["errors"] == 1
    assert next(iter(report["by_agent"])) == "site_manager"  # most expensive first


async def test_budget_status_flags_a_breach(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATA_DIR", str(tmp_path))
    costs.set_budget(0.10, "monthly")
    async with db_sessionmaker() as s:
        s.add(AgentRun(tenant_id="local", workspace_id="local", agent="x", status="ok", cost_usd="0.15"))
        await s.commit()
    status = await costs.budget_status(db_sessionmaker)
    assert status and status["breached"] is True and status["spent_usd"] == pytest.approx(0.15)
