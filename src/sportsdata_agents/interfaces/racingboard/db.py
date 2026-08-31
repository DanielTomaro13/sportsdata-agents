"""
Local SQLite store for the firm-prediction training set.

Forward-collected by the DataLogger (see datalog.py): point-in-time per-runner
snapshots at fixed pre-jump offsets, plus per-runner outcomes once a race resolves.
SQLite (WAL mode) is transactional, so writes are atomic and crash-safe — unlike the
JSON stores — which matters because this is the only copy of the training data.

Schema (all self-contained in RacingBoard; no warehouse dependency):
  races      — one row per race (identity, jump time, field size, results)
  runners    — one row per runner (static identity/form handles)
  snapshots  — one row per (runner, offset bucket): the point-in-time features
  outcomes   — one row per runner: open/horizon/jump prices, firm label, finish
  predictions— one row per (runner, horizon, model): served model scores (later)
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS races (
    race_key   TEXT PRIMARY KEY,
    date       TEXT,
    code       TEXT,
    country    TEXT,
    venue      TEXT,
    race_no    INTEGER,
    race_name  TEXT,
    jump_time  TEXT,
    field_size INTEGER,
    results    TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS runners (
    race_key TEXT,
    number   INTEGER,
    name     TEXT,
    jockey   TEXT,
    trainer  TEXT,
    barrier  INTEGER,
    weight   REAL,
    PRIMARY KEY (race_key, number)
);
CREATE TABLE IF NOT EXISTS snapshots (
    race_key   TEXT,
    number     INTEGER,
    offset_min INTEGER,
    ts         REAL,
    best_price REAL,
    tote_share REAL,
    implied    REAL,
    fair_price REAL,
    price_rank INTEGER,
    n_confirm  INTEGER,
    value_pct  REAL,
    bf_wom     REAL,
    is_tipped  INTEGER,
    direction  TEXT,
    PRIMARY KEY (race_key, number, offset_min)
);
CREATE TABLE IF NOT EXISTS outcomes (
    race_key      TEXT,
    number        INTEGER,
    open_price    REAL,
    horizon_price REAL,
    jump_price    REAL,
    price_move_pct REAL,
    firmed        INTEGER,
    finish_pos    INTEGER,
    won           INTEGER,
    placed        INTEGER,
    PRIMARY KEY (race_key, number)
);
CREATE TABLE IF NOT EXISTS predictions (
    race_key      TEXT,
    number        INTEGER,
    horizon_min   INTEGER,
    p_firm        REAL,
    model_version TEXT,
    generated_at  REAL,
    PRIMARY KEY (race_key, number, horizon_min, model_version)
);
CREATE INDEX IF NOT EXISTS ix_snap_race ON snapshots (race_key);
CREATE INDEX IF NOT EXISTS ix_snap_ts   ON snapshots (ts);
"""


class DB:
    """Thin synchronous wrapper. Inserts are tiny and WAL keeps them sub-ms; a lock
    serialises the single shared connection across the poller's loops."""

    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- writes (INSERT OR IGNORE ⇒ each bucket/runner recorded once) ----
    def upsert_race(self, race_key: str, date: str, code: str, country: str | None,
                    venue: str, race_no: int, race_name: str, jump_time: str,
                    field_size: int) -> None:
        with self._lock:
            # NOT `INSERT OR IGNORE`. A race enters the spine the moment ANY book
            # carries it, and TAB is usually not the first -- it publishes an
            # overseas meeting hours after Betfair has a market up. `country`
            # comes only from TAB, so the first write is very often blank, and
            # OR IGNORE froze that blank in place for the life of the race even
            # though TAB filled in later. 308 of 2,394 races (13%) carried no
            # country because of this, all of them the ones we had not yet met.
            #
            # So: fill blanks as better data arrives, never overwrite something
            # real with something empty. Later is not automatically better --
            # only non-empty is. jump_time is the exception and updates outright,
            # because a re-scheduled race genuinely has a new jump time and the
            # newest report of it is the one worth having.
            self._conn.execute(
                "INSERT INTO races "
                "(race_key,date,code,country,venue,race_no,race_name,jump_time,field_size,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(race_key) DO UPDATE SET "
                "  country    = CASE WHEN COALESCE(races.country,'')   = '' "
                "                    THEN COALESCE(excluded.country, races.country) "
                "                    ELSE races.country END, "
                "  venue      = CASE WHEN COALESCE(races.venue,'')     = '' "
                "                    THEN excluded.venue ELSE races.venue END, "
                "  race_name  = CASE WHEN COALESCE(races.race_name,'') = '' "
                "                    THEN excluded.race_name ELSE races.race_name END, "
                "  field_size = CASE WHEN COALESCE(races.field_size,0) = 0 "
                "                    THEN excluded.field_size ELSE races.field_size END, "
                "  jump_time  = CASE WHEN COALESCE(excluded.jump_time,'') <> '' "
                "                    THEN excluded.jump_time ELSE races.jump_time END",
                (race_key, date, code, country, venue, race_no, race_name, jump_time,
                 field_size, time.time()))
            self._conn.commit()

    def upsert_runners(self, race_key: str, rows: list[tuple]) -> None:
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO runners "
                "(race_key,number,name,jockey,trainer,barrier,weight) VALUES (?,?,?,?,?,?,?)",
                [(race_key, *r) for r in rows])
            self._conn.commit()

    def insert_snapshot(self, race_key: str, number: int, offset_min: int, ts: float,
                        cols: dict[str, Any]) -> bool:
        """Record one runner's point-in-time features for a bucket. Returns False if
        that bucket was already captured (PK conflict) — the caller uses this to know
        the bucket is done."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO snapshots "
                "(race_key,number,offset_min,ts,best_price,tote_share,implied,fair_price,"
                "price_rank,n_confirm,value_pct,bf_wom,is_tipped,direction) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (race_key, number, offset_min, ts,
                 cols.get("best_price"), cols.get("tote_share"), cols.get("implied"),
                 cols.get("fair_price"), cols.get("price_rank"), cols.get("n_confirm"),
                 cols.get("value_pct"), cols.get("bf_wom"),
                 1 if cols.get("is_tipped") else 0, cols.get("direction")))
            self._conn.commit()
            return cur.rowcount > 0

    def has_bucket(self, race_key: str, number: int, offset_min: int) -> bool:
        with self._lock:
            r = self._conn.execute(
                "SELECT 1 FROM snapshots WHERE race_key=? AND number=? AND offset_min=?",
                (race_key, number, offset_min)).fetchone()
            return r is not None

    def race_has_outcomes(self, race_key: str) -> bool:
        with self._lock:
            r = self._conn.execute(
                "SELECT 1 FROM outcomes WHERE race_key=? LIMIT 1", (race_key,)).fetchone()
            return r is not None

    def snapshots_for(self, race_key: str) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT number,offset_min,best_price FROM snapshots WHERE race_key=? "
                "ORDER BY number, offset_min DESC", (race_key,))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def set_results(self, race_key: str, results_json: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE races SET results=? WHERE race_key=?",
                               (results_json, race_key))
            self._conn.commit()

    def insert_outcomes(self, rows: list[tuple]) -> None:
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO outcomes "
                "(race_key,number,open_price,horizon_price,jump_price,price_move_pct,"
                "firmed,finish_pos,won,placed) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            self._conn.commit()

    def insert_predictions(self, rows: list[tuple]) -> None:
        """rows: (race_key, number, horizon_min, p_firm, model_version, generated_at)."""
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO predictions "
                "(race_key,number,horizon_min,p_firm,model_version,generated_at) "
                "VALUES (?,?,?,?,?,?)", rows)
            self._conn.commit()

    def heuristic_record(self) -> dict[str, Any]:
        """Live track record of logged predictions vs outcomes, by score tier —
        plain SQL so the server never needs the ML stack."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT CASE WHEN p.p_firm >= 0.66 THEN 'STRONG'
                            WHEN p.p_firm >= 0.48 THEN 'WARM'
                            ELSE 'LEAN' END AS tier,
                       COUNT(*), AVG(o.firmed), AVG(o.won)
                FROM predictions p
                JOIN outcomes o ON p.race_key = o.race_key AND p.number = o.number
                GROUP BY tier
                """).fetchall()
            base = self._conn.execute(
                """
                SELECT AVG(o.firmed) FROM predictions p
                JOIN outcomes o ON p.race_key = o.race_key AND p.number = o.number
                """).fetchone()[0]
        tiers = {t: {"n": n, "firm_rate": round(fr, 3) if fr is not None else None,
                     "win_rate": round(wr, 3) if wr is not None else None}
                 for t, n, fr, wr in rows}
        return {"tiers": tiers, "base_firm_rate": round(base, 3) if base is not None else None}

    def coverage_by_horizon(self) -> list[dict[str, Any]]:
        """Per pre-jump bucket: snapshots captured, races covered, and how many rows
        already have a labelled outcome — the model page's coverage table."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT s.offset_min, COUNT(*) AS n, COUNT(DISTINCT s.race_key) AS races,
                       SUM(CASE WHEN o.race_key IS NOT NULL THEN 1 ELSE 0 END) AS labelled
                FROM snapshots s
                LEFT JOIN outcomes o ON s.race_key = o.race_key AND s.number = o.number
                GROUP BY s.offset_min ORDER BY s.offset_min DESC
                """).fetchall()
        return [{"offset_min": o, "rows": n, "races": r, "labelled": lab or 0}
                for o, n, r, lab in rows]

    def top_pick_sim(self, limit_recent: int = 14) -> dict[str, Any]:
        """Chronological ½-Kelly simulation from a $100 bank: back every settled
        top-scored runner (same staking rules as the FOLLOWING ledger — p = 1/fair,
        odds = best price at prediction time, positive edge only). Returns the
        summary plus the most recent races with their simulated stake/P&L attached."""
        from .kelly import kelly_stake
        with self._lock:
            rows = self._conn.execute(
                """
                WITH hz AS (SELECT race_key, MAX(horizon_min) AS h FROM predictions GROUP BY race_key),
                top AS (
                    SELECT p.race_key, hz.h, p.number, MAX(p.p_firm) AS pf
                    FROM predictions p JOIN hz ON hz.race_key = p.race_key AND p.horizon_min = hz.h
                    GROUP BY p.race_key
                )
                SELECT r.venue, r.race_no, r.code, r.jump_time,
                       t.number, rn.name, t.pf, t.h,
                       s.best_price, s.fair_price,
                       o.jump_price, o.price_move_pct, o.firmed, o.won
                FROM top t
                JOIN outcomes o ON o.race_key = t.race_key AND o.number = t.number
                JOIN races r ON r.race_key = t.race_key
                LEFT JOIN snapshots s ON s.race_key = t.race_key AND s.number = t.number
                                      AND s.offset_min = t.h
                LEFT JOIN runners rn ON rn.race_key = t.race_key AND rn.number = t.number
                ORDER BY r.jump_time ASC
                """).fetchall()
        cols = ("venue", "race_no", "code", "jump_time", "number", "name", "p_firm",
                "horizon_min", "best_price", "fair_price", "jump_price", "move_pct",
                "firmed", "won")
        picks = [dict(zip(cols, r)) for r in rows]

        bank, staked, bets = 100.0, 0.0, 0
        for e in picks:
            price, fair = e["best_price"], e["fair_price"]
            stake = kelly_stake(bank, (1.0 / fair) if fair else None, price, 0.5, 0.25)
            e["stake"] = stake if stake > 0 else None
            if stake > 0:
                bets += 1
                staked += stake
                pnl = stake * (price - 1) if e["won"] else -stake
                bank += pnl
                e["pnl"] = round(pnl, 2)
            else:
                e["pnl"] = None
        profit = bank - 100.0
        return {
            "summary": {"start": 100.0, "bank": round(bank, 2), "profit": round(profit, 2),
                        "roi": round(100 * profit / staked, 1) if staked else None,
                        "bets": bets, "races": len(picks)},
            "recent": list(reversed(picks[-limit_recent:])),
        }

    def recent_reflections(self, limit: int = 14) -> list[dict[str, Any]]:
        """The most recently settled races, each with its TOP-scored runner at the
        earliest horizon logged — and what actually happened (predicted vs closed)."""
        with self._lock:
            rows = self._conn.execute(
                """
                WITH settled AS (
                    SELECT r.race_key, r.venue, r.race_no, r.code, r.jump_time
                    FROM races r
                    WHERE EXISTS (SELECT 1 FROM outcomes o WHERE o.race_key = r.race_key)
                    ORDER BY r.jump_time DESC LIMIT ?
                ),
                hz AS (SELECT race_key, MAX(horizon_min) AS h FROM predictions GROUP BY race_key)
                SELECT s.venue, s.race_no, s.code, s.jump_time,
                       p.number, rn.name, p.p_firm, p.horizon_min,
                       o.open_price, o.jump_price, o.price_move_pct, o.firmed, o.won
                FROM settled s
                JOIN hz ON hz.race_key = s.race_key
                JOIN predictions p ON p.race_key = s.race_key AND p.horizon_min = hz.h
                JOIN outcomes o ON o.race_key = p.race_key AND o.number = p.number
                LEFT JOIN runners rn ON rn.race_key = p.race_key AND rn.number = p.number
                WHERE p.p_firm = (SELECT MAX(p2.p_firm) FROM predictions p2
                                  WHERE p2.race_key = p.race_key AND p2.horizon_min = hz.h)
                GROUP BY s.race_key
                ORDER BY s.jump_time DESC
                """, (limit,)).fetchall()
        cols = ("venue", "race_no", "code", "jump_time", "number", "name", "p_firm",
                "horizon_min", "open_price", "jump_price", "move_pct", "firmed", "won")
        return [dict(zip(cols, r)) for r in rows]

    def today_reflection(self, date: str) -> dict[str, Any]:
        """Today's rolling accuracy: of races settled today, how often did the
        top-scored runner actually firm (vs the day's base firm-rate)?"""
        with self._lock:
            row = self._conn.execute(
                """
                WITH hz AS (SELECT race_key, MAX(horizon_min) AS h FROM predictions GROUP BY race_key),
                top AS (
                    SELECT p.race_key, p.number, MAX(p.p_firm) AS pf
                    FROM predictions p JOIN hz ON hz.race_key = p.race_key AND p.horizon_min = hz.h
                    GROUP BY p.race_key
                )
                SELECT COUNT(*),
                       SUM(o.firmed), AVG(o.firmed),
                       SUM(o.won), AVG(o.won)
                FROM top t
                JOIN outcomes o ON o.race_key = t.race_key AND o.number = t.number
                JOIN races r ON r.race_key = t.race_key
                WHERE r.date = ?
                """, (date,)).fetchone()
            base = self._conn.execute(
                "SELECT AVG(o.firmed) FROM outcomes o JOIN races r ON r.race_key=o.race_key WHERE r.date=?",
                (date,)).fetchone()[0]
        n, firmed, firm_rate, won, win_rate = row
        return {"races": n or 0, "top_firmed": firmed or 0,
                "top_firm_rate": round(firm_rate, 3) if firm_rate is not None else None,
                "top_won": won or 0,
                "top_win_rate": round(win_rate, 3) if win_rate is not None else None,
                "base_firm_rate": round(base, 3) if base is not None else None}

    def counts(self) -> dict[str, int]:
        with self._lock:
            def n(t: str) -> int:
                return self._conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            return {"races": n("races"), "snapshots": n("snapshots"),
                    "outcomes": n("outcomes"), "predictions": n("predictions")}
