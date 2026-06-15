"""Native desktop window helpers + warehouse self-creation."""

from __future__ import annotations

import socket
import tempfile

import pytest

from sportsdata_agents.app.desktop import _free_port
from sportsdata_agents.data.db import ensure_schema, make_engine

pytestmark = pytest.mark.unit


def test_free_port_returns_preferred_when_open() -> None:
    # find a port that's currently free, then confirm _free_port hands it back
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = int(s.getsockname()[1])
    assert _free_port("127.0.0.1", free) == free


def test_free_port_picks_another_when_taken() -> None:
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        taken = int(held.getsockname()[1])
        chosen = _free_port("127.0.0.1", taken)
    assert chosen != taken and 1024 <= chosen <= 65535


async def test_ensure_schema_creates_tables_on_a_fresh_sqlite() -> None:
    from sqlalchemy import text

    with tempfile.TemporaryDirectory() as d:
        engine = make_engine(f"sqlite+aiosqlite:///{d}/w.db")
        try:
            await ensure_schema(engine)
            async with engine.connect() as conn:
                n = (await conn.execute(
                    text("SELECT count(*) FROM sqlite_master WHERE type='table'")
                )).scalar()
        finally:
            await engine.dispose()
    assert n and n > 10  # the full ORM schema (agent_runs, usage, alerts, …)


async def test_ensure_schema_is_a_noop_for_non_sqlite() -> None:
    # a postgres URL must not trigger create_all (server schema is alembic-managed);
    # ensure_schema returns without ever connecting, so a bogus host can't error.
    engine = make_engine("postgresql+asyncpg://u:p@127.0.0.1:1/none")
    try:
        await ensure_schema(engine)  # returns immediately; no connection attempted
    finally:
        await engine.dispose()
