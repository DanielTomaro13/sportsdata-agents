"""Shared fixtures for data tests — async session with the schema applied.

Default: in-memory SQLite. Set ``TEST_DATABASE_URL`` to run the same suite against
the prod dialect (the CI Postgres job does): the database NAME must contain "test"
— the fixture drops and recreates the whole schema around every test.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import sportsdata_agents.data.models  # noqa: F401  (register tables on Base.metadata)
from sportsdata_agents.data.base import Base


def _db_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", "sqlite+aiosqlite://")
    if not url.startswith("sqlite") and "test" not in url.rsplit("/", 1)[-1]:
        raise RuntimeError(
            "TEST_DATABASE_URL must name a throwaway database containing 'test' — "
            "this fixture DROPS the whole schema around every test"
        )
    return url


@pytest.fixture
async def db_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    url = _db_url()
    kwargs = (
        {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}  # shared in-mem conn
        if url.startswith("sqlite")
        else {}
    )
    engine = create_async_engine(url, **kwargs)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)  # clean slate on shared (Postgres) DBs
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def session(db_sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with db_sessionmaker() as s:
        yield s


@pytest.fixture(autouse=True)
def _broad_test_coverage(monkeypatch):
    """Integration tests exercise many sports; the shipped DEFAULT_COVERAGE is
    the operator's PERSONAL selection and must not decide what tests can see.
    A broad env-var coverage keeps the gates on without coupling tests to it."""
    import json

    from sportsdata_agents.operations.ingestion import coverage as _cov

    monkeypatch.setenv("SPORTSDATA_AGENTS_COVERAGE", json.dumps({
        "australian_rules": [], "rugby_league": [], "baseball": [],
        "basketball": [], "tennis": [], "ice_hockey": [], "cricket": [],
        "rugby_union": [], "mma": [], "golf": [], "darts": [], "snooker": [],
        "horse_racing": [], "thoroughbred_racing": [], "greyhound_racing": [],
        "harness_racing": [], "american_football": [], "nfl": [],
    }))
    _cov._prefs.cache_clear()
    yield
    _cov._prefs.cache_clear()
