"""The desktop SQLite warehouse must converge to the SAME indexes/constraints the Postgres
path gets via alembic — not just columns. A warehouse predating uq_prices_change (0013) must
have it created (deduping any existing duplicate change-points) at launch."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

import sportsdata_agents.data.models  # noqa: F401 — registers tables on Base.metadata
from sportsdata_agents.data.base import Base
from sportsdata_agents.data.db import ensure_schema

pytestmark = pytest.mark.unit


async def test_ensure_schema_backfills_unique_index_and_dedups() -> None:
    eng = create_async_engine("sqlite+aiosqlite://")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
        # simulate an OLD warehouse that predates migration 0013
        await c.exec_driver_sql("DROP INDEX uq_prices_change")
        for _ in range(2):  # two identical change-points — only possible without the index
            await c.exec_driver_sql(
                "INSERT INTO prices (id, changed_at, provider, book, sport, event_external_id,"
                " market, selection, odds) VALUES (?, '2026-06-01T00:00:00+00:00', 'tab', 'TAB',"
                " 'afl', 'E1', 'h2h', 'home', 1.9)",
                (uuid.uuid4().hex,),
            )

    await ensure_schema(eng)

    async with eng.connect() as c:
        idx = (
            await c.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='uq_prices_change'"
            )
        ).all()
        n = (await c.exec_driver_sql("SELECT COUNT(*) FROM prices")).scalar()
    await eng.dispose()
    assert idx, "uq_prices_change should be (re)created on an old warehouse"
    assert n == 1, "the duplicate change-point should be deduped before the unique index"
