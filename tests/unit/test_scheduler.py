"""The conductor: due logic, proximity ladder, locks, failure handoff."""

from __future__ import annotations

import datetime as dt
import subprocess
from typing import Any

import pytest

from sportsdata_agents.operations.scheduler import (
    HEALTH_AFTER_FAILURES,
    JOBS,
    TRIAGE_AFTER_FAILURES,
    Job,
    acquire_lock,
    calendar_due,
    due_jobs,
    interval_due,
    pace_for,
    run_tick,
)

pytestmark = pytest.mark.unit


def test_pace_ladder_tightens_toward_start() -> None:
    assert pace_for(None) is None  # quiet board: base cadence
    assert pace_for(7 * 3600) is None  # 7h out: base cadence
    assert pace_for(5 * 3600) == 1800  # 5h: every 30min
    assert pace_for(90 * 60) == 1200  # 90min: 20min
    assert pace_for(45 * 60) == 900  # 45min: 15min
    assert pace_for(25 * 60) == 600  # 25min: 10min
    assert pace_for(15 * 60) == 300  # 15min: 5min
    assert pace_for(7 * 60) == 180  # 7min: 3min
    assert pace_for(60) == 120  # 1min: as fast as the tick allows
    assert pace_for(-10) is None  # already started: in-play is not ours


def test_interval_due_is_boundary_crossing() -> None:
    # 300s job: due exactly when a 5-minute boundary falls inside the window
    t = dt.datetime(2026, 6, 12, 10, 5, 10).timestamp()
    assert interval_due(300, t, 60)  # 10:05:00 boundary crossed
    assert not interval_due(300, t - 60, 60)  # 10:03:10..10:04:10 — no boundary


def test_calendar_due_daily_and_weekly() -> None:
    nightly = Job(name="n", args=(), log="/tmp/x.log", at=(23, 30))
    weekly = Job(name="w", args=(), log="/tmp/x.log", weekday=0, at=(9, 0))  # Monday
    in_slot = dt.datetime(2026, 6, 12, 23, 30, 20)  # Friday 23:30:20
    assert calendar_due(nightly, in_slot, 60)
    assert not calendar_due(nightly, in_slot + dt.timedelta(minutes=2), 60)
    monday = dt.datetime(2026, 6, 15, 9, 0, 30)  # Monday 09:00:30
    assert calendar_due(weekly, monday, 60)
    assert not calendar_due(weekly, monday + dt.timedelta(days=1), 60)  # Tuesday
    assert not calendar_due(weekly, monday - dt.timedelta(minutes=5), 60)  # too early


def test_registry_covers_the_nine_retired_cron_lines(monkeypatch: Any) -> None:
    monkeypatch.setenv("SPORTSDATA_OPERATOR", "1")  # operator: the full job set runs
    names = {j.name for j in JOBS}
    assert names == {"ingest", "monitor", "slate", "custodian", "resolve", "results",
                     "steward", "eval_benchmark", "site_manager", "refresh_books",
                     "ops_health", "budget_watch"}
    custodian = next(j for j in JOBS if j.name == "custodian")
    assert custodian.interval_s == 3600  # hourly pressure check; the run decides hold/prune
    ingest = next(j for j in JOBS if j.name == "ingest")
    assert ingest.paced and ingest.interval_s == 60
    # one full week at 60s ticks fires every job at least once
    start = dt.datetime(2026, 6, 15, 0, 0, 30)
    fired: set[str] = set()
    for minute in range(7 * 24 * 60):
        fired |= {j.name for j in due_jobs(start + dt.timedelta(minutes=minute), 60)}
    assert fired == names


def test_operator_only_jobs_never_run_on_a_customer_install(monkeypatch: Any) -> None:
    """The platform-maintenance jobs (your site/repo/evals/catalogue) run only on
    the operator's deployment; a customer's conductor runs just the data plane."""
    monkeypatch.delenv("SPORTSDATA_OPERATOR", raising=False)  # default = customer
    operator_only = {"eval_benchmark", "site_manager", "refresh_books", "ops_health",
                     "budget_watch"}
    start = dt.datetime(2026, 6, 15, 0, 0, 30)
    fired: set[str] = set()
    for minute in range(7 * 24 * 60):
        fired |= {j.name for j in due_jobs(start + dt.timedelta(minutes=minute), 60)}
    assert fired == {"ingest", "monitor", "slate", "custodian", "resolve", "results", "steward"}
    assert not (fired & operator_only)


def test_lock_skips_concurrent_run(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("SPORTSDATA_AGENTS_VAR_DIR", str(tmp_path))
    first = acquire_lock("jobx")
    assert first is not None
    assert acquire_lock("jobx") is None  # held by THIS live process — refused
    first.unlink()
    assert acquire_lock("jobx") is not None  # released — reacquirable


def test_empty_lock_is_stale_and_reclaimed(tmp_path: Any, monkeypatch: Any) -> None:
    """A holder that died before writing its pid leaves a 0-byte lock. pid 0
    would signal OUR process group (always alive) — it must count as stale,
    not held (lived: ingest wedged 10 days behind an empty lock)."""
    monkeypatch.setenv("SPORTSDATA_AGENTS_VAR_DIR", str(tmp_path))
    empty = tmp_path / "locks" / "joby.lock"
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.touch()
    reclaimed = acquire_lock("joby")
    assert reclaimed is not None  # empty lock reclaimed, not treated as live
    assert reclaimed.read_text().strip().isdigit()  # and OUR pid is now inside


def test_failure_handoff_to_the_error_agent(tmp_path: Any, monkeypatch: Any) -> None:
    """Consecutive failures: deterministic health first, then ONE triage handoff.
    The self-healing handoff is operator maintenance (runs ops agents, opens PRs)."""
    monkeypatch.setenv("SPORTSDATA_AGENTS_VAR_DIR", str(tmp_path))
    monkeypatch.setenv("SPORTSDATA_OPERATOR", "1")
    job = Job(name="ingest", args=("ingest", "--once"), log=str(tmp_path / "j.log"),
              interval_s=60, paced=True)
    health = Job(name="ops_health", args=("ops", "health"), log=str(tmp_path / "h.log"))
    calls: list[list[str]] = []

    def failing_runner(j: Job, argv: list[str]) -> subprocess.CompletedProcess:
        calls.append(argv)
        ok = j.name in ("ops_health", "incident_triage")
        return subprocess.CompletedProcess(argv, 0 if ok else 1)

    now = dt.datetime(2026, 6, 12, 10, 0, 30)
    for _ in range(TRIAGE_AFTER_FAILURES):
        run_tick(now=now, period_s=60.0, pace=300, jobs=(job, health), runner=failing_runner)

    flat = [" ".join(argv[1:]) for argv in calls]
    assert flat.count("ingest --once --pace 300") == TRIAGE_AFTER_FAILURES  # paced every time
    health_runs = [c for c in flat if c == "ops health"]
    assert len(health_runs) == TRIAGE_AFTER_FAILURES - HEALTH_AFTER_FAILURES + 1
    triage_runs = [c for c in flat if c.startswith("ops run incident_triage")]
    assert len(triage_runs) == 1  # fired at the threshold…
    run_tick(now=now, period_s=60.0, pace=None, jobs=(job, health), runner=failing_runner)
    triage_runs = [" ".join(a[1:]) for a in calls if " ".join(a[1:]).startswith("ops run incident_triage")]
    assert len(triage_runs) == 1  # …and the cooldown stops a money-burning loop

    # recovery resets the counter
    def healthy_runner(j: Job, argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 0)

    report = run_tick(now=now, period_s=60.0, pace=None, jobs=(job, health), runner=healthy_runner)
    assert report.failed == [] and "ingest" in report.ran

    from sportsdata_agents.tools.ops import read_ops_state

    assert read_ops_state()["job_failures"]["ingest"] == 0


def test_paced_feeds_floor_only_the_fast_tiers() -> None:
    """Flooring the 60-minute firehoses made one cycle outlast the racing
    cadence (observed live: racing silent 40+min) — pace scopes to ≤15min."""
    from sportsdata_agents.operations.ingestion import FEEDS
    from sportsdata_agents.operations.ingestion.worker import paced_feeds

    paced = {f.name: f.interval_s for f in paced_feeds(list(FEEDS.values()), 120)}
    assert paced["unibet_all"] == 120  # hot tier accelerates
    assert paced["kalshi_all"] == 120  # prediction tier accelerates
    assert paced["fanduel_racing_win"] == 120  # already fast, floor is a no-op direction
    assert paced["sportsbet_books"] == 3600  # firehose tiers NEVER accelerate
    assert paced["tab_racing_futures"] == 3600
