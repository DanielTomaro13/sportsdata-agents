"""M0.3 — data layer: migration applies, CRUD round-trips, and tenant isolation holds."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sportsdata_agents.data.models import AgentRun, Recommendation
from sportsdata_agents.data.repository import Repository, TenantScope

pytestmark = pytest.mark.integration


async def test_crud_roundtrip(session: AsyncSession) -> None:
    runs = Repository(AgentRun, session, TenantScope("acme", "main"))
    run = await runs.add(agent="odds_specialist", model="claude", tier="balanced")
    assert run.tenant_id == "acme" and run.workspace_id == "main"
    fetched = await runs.get(run.id)
    assert fetched is not None and fetched.id == run.id
    assert fetched.agent == "odds_specialist"
    assert await runs.count() == 1


async def test_tenant_isolation(session: AsyncSession) -> None:
    a = Repository(Recommendation, session, TenantScope("tenant-A", "w"))
    b = Repository(Recommendation, session, TenantScope("tenant-B", "w"))

    rec = await a.add(selection="Pies -10.5", reasoning="model edge", book="pinnacle")

    # B is a different tenant — it must not see A's row, by id or in a list.
    assert await b.get(rec.id) is None
    assert await b.count() == 0
    # A sees its own.
    assert (await a.get(rec.id)) is not None
    assert await a.count() == 1


def test_migrations_apply(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`alembic upgrade head` on a clean SQLite DB creates every §9 table."""
    from sportsdata_agents.config import get_settings

    db = tmp_path / "m.db"
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    get_settings.cache_clear()
    try:
        from alembic import command
        from alembic.config import Config

        command.upgrade(Config("alembic.ini"), "head")

        con = sqlite3.connect(db)
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        con.close()
    finally:
        get_settings.cache_clear()

    expected = {
        "tenants", "workspaces", "users", "memberships", "agent_specs",
        "conversations", "messages", "agent_runs", "tool_calls",
        "usage_ledger", "budgets", "agent_metrics", "memory", "notes",
        "artifacts", "recommendations", "tracked_bets",
        # no "selections": 0014 drops the dead table (models.py explains the
        # denormalized selection-strings design).
        "fixtures", "events", "alembic_version",
    }
    assert expected <= names, f"missing tables: {sorted(expected - names)}"
