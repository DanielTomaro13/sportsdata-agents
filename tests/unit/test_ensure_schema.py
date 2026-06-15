"""ensure_schema brings an existing SQLite warehouse up to the current ORM schema:
it adds missing tables (create_all) AND missing columns (additive ALTER) — so an
older desktop warehouse keeps working after a model grows a column (M4.5)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from sportsdata_agents.data.db import ensure_schema

pytestmark = pytest.mark.unit


async def test_adds_missing_columns_to_existing_table(tmp_path) -> None:
    db = tmp_path / "old.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
    # an OLD warehouse: conversations exists but predates title/archived
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE conversations (id TEXT PRIMARY KEY, tenant_id TEXT, "
            "workspace_id TEXT, created_at TEXT, channel TEXT, external_id TEXT)"
        )
    await ensure_schema(engine)
    async with engine.connect() as conn:
        rows = (await conn.exec_driver_sql("PRAGMA table_info(conversations)")).fetchall()
    await engine.dispose()
    cols = {r[1] for r in rows}
    assert {"title", "archived"} <= cols, f"missing migration columns, have {sorted(cols)}"


async def test_creates_all_tables_on_fresh_db(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    await ensure_schema(engine)
    async with engine.connect() as conn:
        names = {
            r[0]
            for r in (
                await conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
    await engine.dispose()
    assert {"conversations", "messages", "agent_runs"} <= names
