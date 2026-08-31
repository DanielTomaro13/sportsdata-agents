"""
Bounded retention for the training store — review-gated, labels kept forever.

Left alone, db.py grows without bound (~12 MB/day at AU volume: the snapshots
table is ~108 rows per race), which eventually breaks whatever backup budget
the host box has. But the bulk and the value are in different tables:

  snapshots   ~95% of the bytes — point-in-time features, prunable
  outcomes    the labels — one row per runner, ~0.6 MB/day, kept forever
  races/runners/predictions   identity + track record — tiny, kept forever

So retention deletes ONLY snapshots, and only for races older than the window
(default 180 days, ``MF_RETENTION_DAYS``). Steady state ≈ 2 GB instead of
unbounded growth.

THE GATE. Training data cannot be re-collected, so nothing is deleted until
the expiring window has been reviewed:

  1. built-in review (stdlib, always runs): the expiring races must have
     recorded outcomes for at least ``MF_RETENTION_MIN_COVERAGE`` of races
     (default 0.6) — a window that never got labelled is evidence the logger
     was broken, and deleting it would hide that; and a summary of what is
     about to be dropped (row counts, label base rate, per-horizon hit rates
     mirroring firm/evaluate.py) is computed and recorded.
  2. optional model review (``MF_RETENTION_REVIEW_CMD``): when set, the
     command runs first and must exit 0 — point it at the real retrain, e.g.
     ``python -m moneyflow.firm.train --horizon 60``. This module stays
     stdlib-only on purpose: the live server must never need the ML stack
     (see requirements-ml.txt), so the heavy review is invoked, not imported.

  Every run — pruned, held, or empty — is recorded in ``retention_log`` with
  its metrics JSON, so dropped data always leaves an audit trail, and a HELD
  run is visible to monitoring long after the fact.

If the gate fails the prune does not run: the run is logged as HELD and the
process exits 2 (distinct from a crash) so a systemd unit shows as failed and
the host's healthcheck can alert on it.

Run:      python -m moneyflow.retention [--keep-days N] [--dry-run] [--no-vacuum]
Offline:  intended for a timer, off-peak — VACUUM rewrites the file.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import time

from .config import settings

_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS retention_log (
    ts            REAL,
    status        TEXT,     -- PRUNED | HELD | EMPTY | DRY_RUN
    keep_days     INTEGER,
    cutoff_ts     REAL,
    races         INTEGER,  -- candidate races in the expiring window
    snapshots     INTEGER,  -- snapshot rows those races held
    outcome_cover REAL,     -- fraction of candidate races with outcomes
    deleted       INTEGER,  -- snapshot rows actually deleted (0 unless PRUNED)
    metrics       TEXT      -- JSON: what the dropped window contained
);
"""


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    # The collector may be writing (WAL allows one writer + readers); waiting
    # beats failing for a maintenance job that runs off-peak anyway.
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(_LOG_SCHEMA)
    conn.commit()
    return conn


def _window_metrics(conn: sqlite3.Connection, cutoff: float) -> dict:
    """Summarise the expiring window — the audit trail for what gets dropped."""
    q1 = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(n_snap),0) FROM "
        "(SELECT r.race_key, (SELECT COUNT(*) FROM snapshots s WHERE s.race_key=r.race_key) AS n_snap "
        " FROM races r WHERE r.created_at < ?)", (cutoff,)).fetchone()
    n_races, n_snaps = int(q1[0]), int(q1[1])

    q2 = conn.execute(
        "SELECT COUNT(DISTINCT o.race_key) FROM outcomes o "
        "JOIN races r ON r.race_key=o.race_key WHERE r.created_at < ?", (cutoff,)).fetchone()
    covered = int(q2[0])

    base = conn.execute(
        "SELECT AVG(o.firmed), AVG(o.won), COUNT(*) FROM outcomes o "
        "JOIN races r ON r.race_key=o.race_key WHERE r.created_at < ?", (cutoff,)).fetchone()

    # Per-horizon hit rate of logged predictions, same join firm/evaluate.py
    # uses — recorded here because the joined rows are what the prune removes
    # the feature context for.
    hits = conn.execute(
        "SELECT p.horizon_min, p.model_version, COUNT(*), AVG(o.firmed) "
        "FROM predictions p JOIN outcomes o ON o.race_key=p.race_key AND o.number=p.number "
        "JOIN races r ON r.race_key=p.race_key "
        "WHERE r.created_at < ? AND p.p_firm >= 0.5 "
        "GROUP BY p.horizon_min, p.model_version", (cutoff,)).fetchall()

    return {
        "races": n_races,
        "snapshots": n_snaps,
        "races_with_outcomes": covered,
        "outcome_coverage": (covered / n_races) if n_races else 0.0,
        "firmed_base_rate": base[0],
        "win_base_rate": base[1],
        "outcome_rows": int(base[2]),
        "hit_rate_by_horizon": [
            {"horizon_min": h, "model_version": v, "n_scored": int(n), "hit_rate": hr}
            for h, v, n, hr in hits
        ],
    }


def _external_review() -> tuple[bool, str]:
    """Run MF_RETENTION_REVIEW_CMD if configured. Invoked, not imported: the
    retrain needs the ML stack, and this module must not."""
    cmd = os.environ.get("MF_RETENTION_REVIEW_CMD", "").strip()
    if not cmd:
        return True, "no external review configured"
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        return False, f"review command timed out after 1h: {cmd}"
    tail = (proc.stdout + proc.stderr)[-2000:]
    if proc.returncode != 0:
        return False, f"review command exited {proc.returncode}: {tail}"
    return True, tail


def run(keep_days: int, dry_run: bool = False, vacuum: bool = True,
        db_path: str | None = None) -> int:
    path = db_path or settings.db_path
    min_cover = float(os.environ.get("MF_RETENTION_MIN_COVERAGE", "0.6"))
    cutoff = time.time() - keep_days * 86400.0
    conn = _connect(path)
    try:
        metrics = _window_metrics(conn, cutoff)

        def log(status: str, deleted: int = 0) -> None:
            conn.execute(
                "INSERT INTO retention_log VALUES (?,?,?,?,?,?,?,?,?)",
                (time.time(), status, keep_days, cutoff, metrics["races"],
                 metrics["snapshots"], metrics["outcome_coverage"], deleted,
                 json.dumps(metrics)))
            conn.commit()

        if metrics["races"] == 0:
            log("EMPTY")
            print(f"retention: nothing older than {keep_days}d — no-op")
            return 0

        # ---- the gate ----
        if metrics["outcome_coverage"] < min_cover:
            log("HELD")
            print(f"retention: HELD — only {metrics['outcome_coverage']:.0%} of the "
                  f"{metrics['races']} expiring races have outcomes (need {min_cover:.0%}). "
                  "The window looks under-labelled; refusing to delete evidence.")
            return 2

        ok, review_note = _external_review()
        if not ok:
            metrics["review"] = review_note
            log("HELD")
            print(f"retention: HELD — external review failed:\n{review_note}")
            return 2
        metrics["review"] = review_note

        if dry_run:
            log("DRY_RUN")
            print(f"retention: DRY RUN — would delete {metrics['snapshots']} snapshot rows "
                  f"across {metrics['races']} races (labels kept)")
            return 0

        # ---- the prune: snapshots only, candidate races only ----
        cur = conn.execute(
            "DELETE FROM snapshots WHERE race_key IN "
            "(SELECT race_key FROM races WHERE created_at < ?)", (cutoff,))
        deleted = cur.rowcount
        conn.commit()
        log("PRUNED", deleted)
        print(f"retention: deleted {deleted} snapshot rows across {metrics['races']} races; "
              f"kept {metrics['outcome_rows']} outcome rows (labels are forever)")

        if vacuum and deleted:
            # Without this SQLite returns no pages to the filesystem and the
            # prune frees nothing on disk. It rewrites the file — off-peak
            # only, and pointless when nothing was deleted.
            conn.execute("VACUUM")
            print("retention: VACUUM complete")
        return 0
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Prune old snapshot rows (review-gated; labels kept forever)")
    ap.add_argument("--keep-days", type=int,
                    default=int(os.environ.get("MF_RETENTION_DAYS", "180")))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-vacuum", action="store_true")
    ap.add_argument("--db", default=None, help="override settings.db_path")
    a = ap.parse_args()
    return run(a.keep_days, dry_run=a.dry_run, vacuum=not a.no_vacuum, db_path=a.db)


if __name__ == "__main__":
    raise SystemExit(main())
