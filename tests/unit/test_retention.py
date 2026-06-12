"""The data custodian: pressure ladder, hold-vs-prune, backups, safety rails."""

from __future__ import annotations

import datetime as dt
import gzip
from pathlib import Path
from typing import Any

import pytest

from sportsdata_agents.operations import retention

pytestmark = pytest.mark.unit


def test_ladder_holds_with_space_and_tightens_under_pressure() -> None:
    assert retention.plan_retention(80.0) is None  # plenty of space: hold and wait
    assert retention.plan_retention(25.1) is None
    assert retention.plan_retention(24.0) == 60
    assert retention.plan_retention(18.0) == 45
    assert retention.plan_retention(12.0) == 30
    assert retention.plan_retention(7.0) == 21
    assert retention.plan_retention(2.0) == 14


def test_sqlite_path_extraction() -> None:
    assert retention.sqlite_path("sqlite+aiosqlite:////tmp/x.db") == Path("/tmp/x.db")
    assert retention.sqlite_path("sqlite+aiosqlite://") is None  # in-memory
    assert retention.sqlite_path("postgresql+asyncpg://h/db") is None  # not ours to manage


async def _seed_warehouse(db_url: str) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from sportsdata_agents.data.base import Base
    from sportsdata_agents.operations.ingestion import PricePoint, record_points

    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    old = dt.datetime.now(dt.UTC) - dt.timedelta(days=90)
    fresh = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    point = PricePoint(provider="tab", book="TAB", sport="afl", event_external_id="E1",
                       event_name="A v B", market="h2h", selection="home", odds=1.9)
    await record_points(sf, [point], captured_at=old)
    await record_points(sf, [PricePoint(provider="tab", book="TAB", sport="afl",
                                        event_external_id="E1", event_name="A v B",
                                        market="h2h", selection="home", odds=2.0)],
                        captured_at=fresh)
    await engine.dispose()


async def test_custodian_holds_when_space_is_plentiful(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATA_DIR", str(tmp_path))
    db_url = f"sqlite+aiosqlite:///{tmp_path}/wh.db"
    await _seed_warehouse(db_url)
    monkeypatch.setattr(retention, "disk_status",
                        lambda p: {"db_bytes": 1000, "free_bytes": 10**12, "free_pct": 60.0})
    report = await retention.run_custodian(db_url)
    assert report["action"] == "hold" and report["keep_days"] is None
    # first contact: the weekly backup still lands (gzip, rotated)
    backups = list((tmp_path / "backups").glob("warehouse-*.db.gz"))
    assert len(backups) == 1
    with gzip.open(backups[0]) as fh:
        assert fh.read(16)  # readable archive
    # second pass inside the week: no second backup
    report = await retention.run_custodian(db_url)
    assert len(list((tmp_path / "backups").glob("warehouse-*.db.gz"))) == 1


async def test_custodian_prunes_under_pressure_and_never_touches_prices(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATA_DIR", str(tmp_path))
    db_url = f"sqlite+aiosqlite:///{tmp_path}/wh.db"
    await _seed_warehouse(db_url)
    # 12% free → 30-day window; the 90-day-old snapshot goes, yesterday's stays
    monkeypatch.setattr(retention, "disk_status",
                        lambda p: {"db_bytes": 1000, "free_bytes": 10**9, "free_pct": 12.0})
    report = await retention.run_custodian(db_url)
    assert report["action"] == "prune" and report["keep_days"] == 30
    assert report["pruned"] == 1
    assert report["backup"].endswith(".db.gz")  # backup BEFORE the prune

    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from sportsdata_agents.data.models import OddsSnapshot, Price

    engine = create_async_engine(db_url)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as session:
        snaps = (await session.execute(select(func.count()).select_from(OddsSnapshot))).scalar_one()
        prices = (await session.execute(select(func.count()).select_from(Price))).scalar_one()
    await engine.dispose()
    assert snaps == 1  # the fresh capture survives
    assert prices == 2  # the change-point series the models read is NEVER pruned


def test_backup_refuses_to_eat_the_last_headroom(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATA_DIR", str(tmp_path))
    db = tmp_path / "wh.db"
    db.write_bytes(b"x" * 10_000)
    monkeypatch.setattr(retention, "disk_status",
                        lambda p: {"db_bytes": 10_000, "free_bytes": 100, "free_pct": 1.0})
    assert retention.backup_warehouse(db) is None  # skipped, not forced


async def test_hourly_runs_in_a_low_disk_tier_stay_quiet(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A box parked under pressure must not backup/VACUUM/page the operator
    every hour — each heavy action carries its own cadence."""
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATA_DIR", str(tmp_path))
    db_url = f"sqlite+aiosqlite:///{tmp_path}/wh.db"
    await _seed_warehouse(db_url)
    monkeypatch.setattr(retention, "disk_status",
                        lambda p: {"db_bytes": 1000, "free_bytes": 10**9, "free_pct": 7.0})
    pushes: list[str] = []

    async def fake_broadcast(text: str) -> dict[str, bool]:
        pushes.append(text)
        return {"slack": True}

    import sportsdata_agents.observability.notify as notify
    monkeypatch.setattr(notify, "operator_broadcast", fake_broadcast)

    t0 = dt.datetime.now(dt.UTC)
    first = await retention.run_custodian(db_url, now=t0)
    assert first["action"] == "prune" and first["pruned"] == 1
    assert first["backup"].endswith(".db.gz") and first.get("vacuumed") is True
    assert first.get("escalated") is True and len(pushes) == 1

    # one hour later, same tier: no second backup, no VACUUM, no page
    second = await retention.run_custodian(db_url, now=t0 + dt.timedelta(hours=1))
    assert second["action"] == "prune"
    assert "backup" not in second  # daily cadence holds it
    assert "vacuumed" not in second  # weekly cadence (and nothing left to prune)
    assert "escalated" not in second and len(pushes) == 1  # daily cooldown
    assert len(list((tmp_path / "backups").glob("warehouse-*.db.gz"))) == 1
