"""The data custodian: adaptive, disk-aware retention.

Deterministic by design — an LLM never decides what data dies. The conductor
checks in hourly; the custodian decides whether to ACT from disk pressure:

- plenty of space → **hold and wait** (a weekly backup still happens);
- tightening → prune the prunable raw series (``odds_snapshots``) on a sliding
  window — the change-point ``prices`` series the models read is NEVER touched;
- critical → shorter windows, and the operator is told (Slack/Discord).

Every prune is preceded by a gzip-compressed backup (rotated, and skipped with
a warning when the disk can't safely hold one), and the database file only
shrinks via VACUUM when there's comfortable headroom to run one — VACUUM needs
working space, and forcing it on a nearly-full disk is how you lose the DB.
"""

from __future__ import annotations

import datetime as dt
import gzip
import logging
import shutil
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# (free_pct ABOVE this → this action) — first match from the top wins.
# None = hold and wait: with space to spare, deleting data buys nothing.
RETENTION_LADDER: tuple[tuple[float, int | None], ...] = (
    (25.0, None),
    (20.0, 60),
    (15.0, 45),
    (10.0, 30),
    (5.0, 21),
    (0.0, 14),
)
ESCALATE_BELOW_PCT = 10.0  # the operator hears about it before it becomes an outage
ESCALATE_COOLDOWN_S = 24 * 3600  # …once a day, not once an hour
BACKUP_KEEP = 3
WEEKLY_BACKUP_S = 7 * 24 * 3600
PRUNE_BACKUP_S = 24 * 3600  # a prune that deletes rows wants a backup ≤1 day old
VACUUM_MIN_INTERVAL_S = 7 * 24 * 3600  # VACUUM is heavy I/O against the live writer
VACUUM_HEADROOM = 1.3  # VACUUM only when free > db_size * this
GZ_RATIO_GUESS = 0.20  # text-heavy sqlite compresses well; sizing guard only


def sqlite_path(database_url: str) -> Path | None:
    """The on-disk file behind a sqlite URL; None for anything else (Postgres
    manages its own storage — the custodian only prunes there)."""
    marker = "sqlite+aiosqlite:///"
    if marker not in database_url:
        return None
    path = database_url.split(marker, 1)[1]
    return Path(path) if path and path != ":memory:" else None


def disk_status(db_path: Path) -> dict[str, Any]:
    usage = shutil.disk_usage(db_path.parent)
    return {
        "db_bytes": db_path.stat().st_size if db_path.exists() else 0,
        "free_bytes": usage.free,
        "free_pct": round(usage.free / usage.total * 100, 1),
    }


def plan_retention(free_pct: float) -> int | None:
    """keep-days for the current pressure; None = hold and wait."""
    for floor, keep_days in RETENTION_LADDER:
        if free_pct > floor:
            return keep_days
    return RETENTION_LADDER[-1][1]


def backups_dir() -> Path:
    from sportsdata_agents.paths import backups_dir as _backups

    return _backups()


def backup_warehouse(db_path: Path, *, keep: int = BACKUP_KEEP) -> Path | None:
    """A consistent gzip backup via the sqlite backup API (safe against the live
    WAL writer); oldest rotated out. None (warned) when the disk can't safely
    hold one — never trade the live DB's headroom for a copy of itself."""
    status = disk_status(db_path)
    estimated = int(status["db_bytes"] * GZ_RATIO_GUESS)
    if status["free_bytes"] < estimated * 2:
        logger.warning("backup skipped: ~%dMB needed, %dMB free",
                       estimated // 2**20, status["free_bytes"] // 2**20)
        return None
    target = backups_dir() / f"warehouse-{dt.datetime.now(dt.UTC):%Y%m%d-%H%M%S}.db.gz"
    tmp = target.with_suffix(".tmp")
    source = sqlite3.connect(db_path, timeout=60)
    try:
        dest = sqlite3.connect(tmp)
        try:
            source.backup(dest)  # consistent even against the live WAL writer
        finally:
            dest.close()
    finally:
        source.close()
    with open(tmp, "rb") as fh, gzip.open(target, "wb", compresslevel=6) as out:
        shutil.copyfileobj(fh, out, length=2**20)
    tmp.unlink(missing_ok=True)
    existing = sorted(backups_dir().glob("warehouse-*.db.gz"))
    for old in existing[:-keep]:
        old.unlink(missing_ok=True)
    logger.info("backup written: %s (%dMB)", target.name, target.stat().st_size // 2**20)
    return target


def maybe_vacuum(db_path: Path) -> bool:
    """Reclaim file space only with comfortable headroom — pruned pages get
    reused either way, so VACUUM is an optimisation, never a necessity."""
    status = disk_status(db_path)
    if status["free_bytes"] <= status["db_bytes"] * VACUUM_HEADROOM:
        return False
    conn = sqlite3.connect(db_path, timeout=120)
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()
    return True


async def run_custodian(
    database_url: str,
    *,
    force_days: int | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """One custodian pass: measure pressure → hold, or backup+prune(+vacuum)."""
    from sportsdata_agents.tools.ops import read_ops_state, write_ops_state

    now = now or dt.datetime.now(dt.UTC)
    db_path = sqlite_path(database_url)
    report: dict[str, Any] = {"action": "hold"}
    state = read_ops_state()
    custodian = dict(state.get("custodian") or {})

    if db_path is None or not db_path.exists():
        report["note"] = "non-sqlite or missing warehouse — nothing to manage locally"
        return report

    status = disk_status(db_path)
    report["disk"] = status
    keep_days = force_days if force_days is not None else plan_retention(status["free_pct"])
    report["keep_days"] = keep_days

    def _age_s(key: str) -> float:
        stamp = custodian.get(key)
        if stamp is None:
            return float("inf")
        return (now - dt.datetime.fromisoformat(stamp).replace(tzinfo=dt.UTC)).total_seconds()

    # the custodian runs HOURLY — every heavy action carries its own cadence so a
    # box parked in a low-disk tier doesn't backup/VACUUM/page the operator 24x a day
    backup_due = _age_s("last_backup_at") > (
        PRUNE_BACKUP_S if keep_days is not None else WEEKLY_BACKUP_S
    )

    if backup_due:
        path = backup_warehouse(db_path)
        report["backup"] = str(path) if path else "skipped (headroom)"
        if path:
            custodian["last_backup_at"] = now.isoformat()

    if keep_days is not None:
        report["action"] = "prune"

        from sportsdata_agents.data.db import make_engine, make_sessionmaker
        from sportsdata_agents.operations.ingestion import prune_snapshots

        engine = make_engine(database_url)
        try:
            report["pruned"] = await prune_snapshots(
                make_sessionmaker(engine), older_than_days=keep_days
            )
        finally:
            await engine.dispose()
        if report["pruned"] and _age_s("last_vacuum_at") > VACUUM_MIN_INTERVAL_S:
            report["vacuumed"] = maybe_vacuum(db_path)
            if report["vacuumed"]:
                custodian["last_vacuum_at"] = now.isoformat()
        report["disk_after"] = disk_status(db_path)

        if status["free_pct"] < ESCALATE_BELOW_PCT and _age_s("last_escalated_at") > ESCALATE_COOLDOWN_S:
            from sportsdata_agents.observability.notify import operator_broadcast

            await operator_broadcast(
                f":floppy_disk: custodian: disk at {status['free_pct']}% free — pruned "
                f"{report.get('pruned', 0)} snapshots to a {keep_days}d window. "
                f"The Postgres move (POST_DEV.md) retires this pressure for good."
            )
            custodian["last_escalated_at"] = now.isoformat()
            report["escalated"] = True

    custodian["last_run_at"] = now.isoformat()
    custodian["last_report"] = {k: v for k, v in report.items() if k != "disk_after"}
    state["custodian"] = custodian
    write_ops_state(state)
    return report
