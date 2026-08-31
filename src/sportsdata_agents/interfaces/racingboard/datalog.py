"""
DataLogger — forward-collects the firm-prediction training set from the live board.

Tapping the same race_detail the scorer/ledger see, it records one point-in-time
feature snapshot per active runner at each configured pre-jump offset bucket
(T‑120…T‑2), and once a race resolves derives per-runner outcomes (open/horizon/
jump prices, the firm label, finishing position). Everything lands in the local
SQLite store (db.py). This is the sole source of training data — the warehouse was
audited empty — so it must run for weeks before a model can train.

Idempotent: each (runner, bucket) and each race's outcomes are written once
(SQLite PK + an in-memory guard), so re-observing a race is a no-op.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings
from .db import DB
from .firm.heuristic import firm_scores

# A bucket is captured the first time minutes-to-jump drops to at/just-below it,
# but only if we're within this many minutes of the mark — so a race that enters
# tracking part-way through doesn't back-fill already-passed buckets with late data.
_CAPTURE_TOL_MIN = 3.0

# outcomes.horizon_price is recorded at this one offset — the flagship model
# horizon. Other horizons reconstruct their H price from snapshots directly.
_FLAGSHIP_HORIZON = 60


def _minutes_to_jump(start_time: str | None) -> float | None:
    if not start_time:
        return None
    try:
        jump = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
    except Exception:
        return None
    return (jump - datetime.now(timezone.utc)).total_seconds() / 60.0


class DataLogger:
    def __init__(self, db: DB) -> None:
        self.db = db
        self.buckets = sorted(settings.datalog_buckets)          # ascending
        self.threshold = settings.firm_threshold
        self._seen_race: set[str] = set()                        # race/runners recorded
        self._captured: dict[str, set[int]] = {}                 # race_key -> buckets done
        self._settled: set[str] = set()                          # outcomes written

    def observe(self, race_key: str, detail: dict[str, Any]) -> None:
        # Betfair-fallback races (TAB down) have no tote, no corporates and —
        # crucially — no TAB results, so they can never settle: capturing them
        # would seed the training store with permanently label-less rows and
        # drag the retention prune's coverage gate down. Exchange-only data is
        # a different regime; the model's world is the TAB spine.
        if ((detail.get("ref") or {}).get("location")) == "BF":
            return
        status = detail.get("status")
        results = detail.get("results")
        if status == "RESULTED" and results:
            self._settle(race_key, detail, results)
        elif status == "OPEN":
            self._capture(race_key, detail)

    # ---- snapshots while OPEN ----
    def _capture(self, race_key: str, detail: dict[str, Any]) -> None:
        mins = _minutes_to_jump((detail.get("ref") or {}).get("start_time"))
        if mins is None:
            return
        # Which bucket, if any, are we at right now? The smallest bucket >= mins is
        # the next mark we've just reached; capture it once, near its time.
        elig = [b for b in self.buckets if b >= mins]
        if not elig:
            return
        target = min(elig)
        done = self._captured.setdefault(race_key, set())
        if target in done or (target - mins) > _CAPTURE_TOL_MIN:
            return

        runners = [r for r in detail.get("runners", []) if not r.get("scratched")]
        if not runners:
            return
        self._ensure_race(race_key, detail, runners)

        tips = detail.get("tips") or {}
        tip_nums = set(tips.get("numbers") or [])
        # price rank (1 = shortest) among runners that have a backable price
        priced = sorted(
            (r for r in runners if _price(r) is not None), key=lambda r: _price(r))
        rank = {id(r): i + 1 for i, r in enumerate(priced)}

        ts = detail.get("ts")
        for r in runners:
            self.db.insert_snapshot(race_key, r["number"], target, ts, {
                "best_price": _price(r),
                "tote_share": r.get("tote_pool_share"),
                "implied": r.get("bf_implied"),
                "fair_price": r.get("fair_price"),
                "price_rank": rank.get(id(r)),
                "n_confirm": r.get("confirm"),
                "value_pct": r.get("value_pct"),
                "bf_wom": r.get("bf_wom"),
                "is_tipped": (r["number"] in tip_nums) or bool(r.get("best_bet")),
                "direction": r.get("direction"),
            })
        # Log the heuristic firm-score as a prediction at this horizon, so it's
        # scored against outcomes exactly like the future ML model (= the baseline).
        scores = firm_scores(runners, tip_nums)
        self.db.insert_predictions([
            (race_key, num, target, s["score"], "heuristic_v1.1", ts)
            for num, s in scores.items()])
        done.add(target)

    def _ensure_race(self, race_key: str, detail: dict[str, Any], runners: list[dict]) -> None:
        if race_key in self._seen_race:
            return
        ref = detail.get("ref") or {}
        self.db.upsert_race(
            race_key, ref.get("date"), ref.get("code"), ref.get("location"),
            ref.get("venue"), ref.get("race_no"), ref.get("race_name"),
            ref.get("start_time"), len(runners))
        self.db.upsert_runners(race_key, [
            (r["number"], r.get("name"), r.get("jockey"), r.get("trainer"),
             r.get("barrier"), r.get("weight")) for r in runners])
        self._seen_race.add(race_key)

    # ---- outcomes once RESULTED ----
    def _settle(self, race_key: str, detail: dict[str, Any], results: list[int]) -> None:
        if race_key in self._settled or self.db.race_has_outcomes(race_key):
            self._settled.add(race_key)
            return
        rows = self.db.snapshots_for(race_key)   # ordered by number, offset_min DESC
        if not rows:
            return
        # Group each runner's captured prices by offset (largest offset = open,
        # smallest = jump, 60 = horizon).
        by_runner: dict[int, list[dict]] = {}
        for row in rows:
            by_runner.setdefault(row["number"], []).append(row)

        placed = set(results[:3])
        winners = set(detail.get("winners") or results[:1])   # dead-heat aware
        out_rows = []
        for num, series in by_runner.items():
            prices = [s for s in series if s["best_price"]]
            if not prices:
                continue
            open_p = prices[0]["best_price"]            # largest offset (earliest)
            jump_p = prices[-1]["best_price"]           # smallest offset (latest)
            horizon_p = next((s["best_price"] for s in prices
                              if s["offset_min"] == _FLAGSHIP_HORIZON), None)
            move = (jump_p / open_p - 1.0) * 100.0 if open_p else None
            firmed = 1 if (move is not None and move <= -self.threshold * 100.0) else 0
            finish = results.index(num) + 1 if num in results else None
            out_rows.append((
                race_key, num, open_p, horizon_p, jump_p,
                round(move, 2) if move is not None else None, firmed,
                finish, 1 if num in winners else 0,
                1 if num in placed else 0))

        self.db.insert_outcomes(out_rows)
        self.db.set_results(race_key, json.dumps(results))
        self._settled.add(race_key)

    def prune(self, keep_keys: set[str]) -> None:
        """Drop in-memory tracking for races no longer on the board (their rows are
        already safely in SQLite) so the sets don't grow for the process lifetime."""
        for d in (self._captured,):
            for k in list(d):
                if k not in keep_keys:
                    del d[k]
        self._seen_race &= keep_keys
        self._settled &= keep_keys

    def stats(self) -> dict[str, int]:
        return self.db.counts()

    def overview(self) -> dict[str, Any]:
        """Slow-changing training/model status for the site: dataset size, the
        heuristic's live track record, and the latest offline training run (read
        from the metrics file train.py writes — the server never runs ML)."""
        today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        sim = self.db.top_pick_sim()
        out: dict[str, Any] = {"counts": self.db.counts(), **self.db.heuristic_record(),
                               "coverage": self.db.coverage_by_horizon(),
                               "reflection": {"recent": sim["recent"],
                                              "sim": sim["summary"],
                                              "today": self.db.today_reflection(today)}}
        latest = None
        models_dir = Path(__file__).parent / "models"
        for p in models_dir.glob("metrics_h*.json"):
            try:
                m = json.loads(p.read_text())
            except Exception:
                continue
            if latest is None or (m.get("trained_at") or 0) > (latest.get("trained_at") or 0):
                latest = m
        out["last_train"] = latest
        return out


def _price(r: dict[str, Any]) -> float | None:
    return r.get("corp_best") or r.get("fixed_win") or r.get("tote_win")
