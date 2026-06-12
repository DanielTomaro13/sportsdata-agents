"""The conductor: ONE managed entry point instead of nine cron lines.

``agents schedule --cron 60`` ticks once a minute (the only crontab entry).
Each tick is deterministic — no LLM in the dispatch path:

1. **Ingest with event-proximity pacing**: the closer the nearest upcoming
   fixture, the faster the hot feeds re-capture — 6h+ out rides each feed's
   base cadence; inside 6h a pace floor kicks in and tightens as start
   approaches (30 → 20 → 15 → 10 → 5 → 3 → 2 minutes). Racing keeps its own
   fast cadence; the floor only ever SPEEDS a feed up.
2. **Fixed-schedule jobs** (resolve+results nightly, steward/eval/site weekly,
   refresh-books/health Sunday) fire when the tick window crosses their
   wall-clock slot — stateless boundary logic, so a missed tick never
   double-fires and a dead box resumes cleanly.
3. **Monitor** (the arb watch et al) every 5 minutes; the **custodian** checks
   disk pressure hourly and holds/backs-up/prunes adaptively.
4. **Failure handoff to the error agent**: consecutive failures per job are
   durable in ops_state; two in a row triggers an immediate deterministic
   ``ops health``; three hands the job to the ``incident_triage`` ops agent
   (remediate within its allow-list or escalate to Slack), rate-limited to
   once per 6h per job so a hard-down feed cannot burn money in a loop.

Per-job lock files stop overlapping runs — a slow ingest cycle never stacks
behind the next tick. Job subprocess output appends to the same per-job logs
the old cron lines used.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ─── the job registry (mirrors the nine retired cron lines) ────────────────


@dataclass(frozen=True)
class Job:
    name: str
    args: tuple[str, ...]  # `agents <args...>`
    log: str  # append-mode log path
    # interval jobs fire on wall-clock boundary crossings of interval_s;
    # calendar jobs fire when the tick window crosses (weekday, hour, minute)
    interval_s: int | None = None
    weekday: int | None = None  # 0=Monday … 6=Sunday; None = daily
    at: tuple[int, int] | None = None  # (hour, minute) local time
    timeout_s: int = 1800
    paced: bool = False  # ingest only: gets the proximity --pace flag


JOBS: tuple[Job, ...] = (
    Job(name="ingest", args=("ingest", "--once", "--cron", "60"),
        log="/tmp/agents-ingest.log", interval_s=60, timeout_s=3000, paced=True),
    Job(name="monitor", args=("monitor",),
        log="/tmp/agents-monitor.log", interval_s=300, timeout_s=600),
    Job(name="custodian", args=("custodian",),
        log="/tmp/agents-cron.log", interval_s=3600, timeout_s=1800),
    Job(name="resolve", args=("resolve",),
        log="/tmp/agents-cron.log", at=(23, 30), timeout_s=1800),
    Job(name="results", args=("results",),
        log="/tmp/agents-cron.log", at=(23, 40), timeout_s=1800),
    Job(name="steward", args=("steward",),
        log="/tmp/agents-steward.log", weekday=0, at=(9, 0), timeout_s=1800),
    Job(name="eval_benchmark",
        args=("ops", "run", "eval_benchmark",
              "Run your standing weekly evaluation: offline evals vs baseline, "
              "agent_metrics rollups, and the delegation_stats routing-economics report."),
        log="/tmp/agents-ops.log", weekday=0, at=(9, 30), timeout_s=1800),
    Job(name="site_manager",
        args=("ops", "run", "site_manager",
              "Weekly site run: check status, audit against the catalogue, and post "
              "the traffic report. Propose a PR only if there is real drift."),
        log="/tmp/agents-site-manager.log", weekday=0, at=(10, 0), timeout_s=1800),
    Job(name="refresh_books", args=("refresh-books",),
        log="/tmp/agents-cron.log", weekday=6, at=(6, 0), timeout_s=1800),
    Job(name="ops_health", args=("ops", "health"),
        log="/tmp/agents-cron.log", weekday=6, at=(7, 0), timeout_s=900),
)

# ─── event-proximity pacing ────────────────────────────────────────────────

# (seconds-to-nearest-start ceiling, pace floor in seconds) — first match wins.
# Daniel's ladder: ~6h out every 30min, tightening to as-fast-as-the-tick as
# the match approaches. The floor only SPEEDS feeds up (min with base cadence).
PACE_LADDER: tuple[tuple[int, int], ...] = (
    (5 * 60, 120),       # <5min out: every 2min
    (10 * 60, 180),      # <10min: 3min
    (20 * 60, 300),      # <20min: 5min
    (30 * 60, 600),      # <30min: 10min
    (60 * 60, 900),      # <1h: 15min
    (2 * 3600, 1200),    # <2h: 20min
    (6 * 3600, 1800),    # <6h: 30min
)


def pace_for(seconds_to_start: float | None) -> int | None:
    """The ingest pace floor for the nearest upcoming fixture; None = base."""
    if seconds_to_start is None or seconds_to_start < 0:
        return None
    for ceiling, floor in PACE_LADDER:
        if seconds_to_start <= ceiling:
            return floor
    return None


async def seconds_to_nearest_start(session_factory: Any) -> float | None:
    """Seconds until the next known fixture start within 6h; None when quiet."""
    from sqlalchemy import func, select

    from sportsdata_agents.data.models import Fixture

    now = dt.datetime.now(dt.UTC)
    horizon = now + dt.timedelta(hours=6)
    async with session_factory() as session:
        nearest = (
            await session.execute(
                select(func.min(Fixture.start_time)).where(
                    Fixture.start_time > now.replace(tzinfo=None),
                    Fixture.start_time <= horizon.replace(tzinfo=None),
                )
            )
        ).scalar()
    if nearest is None:
        return None
    if nearest.tzinfo is None:
        nearest = nearest.replace(tzinfo=dt.UTC)
    return (nearest - now).total_seconds()


# ─── due logic (stateless: wall clock, never a state file) ─────────────────


def interval_due(interval_s: int, now_s: float, period_s: float) -> bool:
    """A boundary of ``interval_s`` was crossed in the last ``period_s``."""
    return int(now_s // interval_s) != int((now_s - period_s) // interval_s)


def calendar_due(job: Job, now: dt.datetime, period_s: float) -> bool:
    """The job's (weekday, hh:mm) slot falls inside (now - period, now]."""
    assert job.at is not None
    hour, minute = job.at
    slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if job.weekday is not None:
        slot -= dt.timedelta(days=(slot.weekday() - job.weekday) % 7)
    if slot > now:
        slot -= dt.timedelta(days=7 if job.weekday is not None else 1)
    return (now - slot).total_seconds() < period_s


def due_jobs(now: dt.datetime, period_s: float, jobs: Sequence[Job] = JOBS) -> list[Job]:
    out = []
    for job in jobs:
        if job.interval_s is not None:
            if interval_due(job.interval_s, now.timestamp(), period_s):
                out.append(job)
        elif job.at is not None and calendar_due(job, now, period_s):
            out.append(job)
    return out


# ─── durable job state + the error-agent handoff ───────────────────────────

HEALTH_AFTER_FAILURES = 2  # consecutive failures → immediate deterministic health check
TRIAGE_AFTER_FAILURES = 3  # … → hand the job to the incident_triage ops agent
TRIAGE_COOLDOWN_S = 6 * 3600  # at most one triage run per job per 6h


def record_outcome(job_name: str, *, ok: bool, returncode: int, duration_s: float) -> int:
    """Persist the run outcome in ops_state; returns the consecutive-failure count."""
    from sportsdata_agents.tools.ops import read_ops_state, write_ops_state

    state = read_ops_state()
    failures = dict(state.get("job_failures") or {})
    runs = dict(state.get("job_runs") or {})
    count = 0 if ok else int(failures.get(job_name, 0)) + 1
    failures[job_name] = count
    runs[job_name] = {
        "at": dt.datetime.now(dt.UTC).isoformat(),
        "ok": ok,
        "returncode": returncode,
        "duration_s": round(duration_s, 1),
    }
    state["job_failures"] = failures
    state["job_runs"] = runs
    write_ops_state(state)
    return count


def triage_allowed(job_name: str) -> bool:
    """Rate-limit the LLM handoff: once per job per cooldown window."""
    from sportsdata_agents.tools.ops import read_ops_state, write_ops_state

    state = read_ops_state()
    last = dict(state.get("last_triage_at") or {})
    now = dt.datetime.now(dt.UTC)
    previous = last.get(job_name)
    if previous:
        prev_at = dt.datetime.fromisoformat(previous)
        if prev_at.tzinfo is None:
            prev_at = prev_at.replace(tzinfo=dt.UTC)
        if (now - prev_at).total_seconds() < TRIAGE_COOLDOWN_S:
            return False
    last[job_name] = now.isoformat()
    state["last_triage_at"] = last
    write_ops_state(state)
    return True


# ─── the runner ────────────────────────────────────────────────────────────


def _agents_binary() -> str:
    return str(Path(sys.executable).parent / "agents")


def _lock_dir() -> Path:
    base = os.environ.get("SPORTSDATA_AGENTS_VAR_DIR") or str(Path.home() / ".sportsdata-agents")
    path = Path(base) / "locks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def acquire_lock(job_name: str) -> Path | None:
    """O_EXCL lock per job; a lock whose pid is dead is stale and reclaimed."""
    path = _lock_dir() / f"{job_name}.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            pid = int(path.read_text().strip() or 0)
            os.kill(pid, 0)  # raises if the holder is gone
            return None  # held by a live process — skip this run
        except (ValueError, ProcessLookupError, PermissionError):
            path.unlink(missing_ok=True)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                return None  # lost the reclaim race
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return path


Runner = Callable[[Job, list[str]], subprocess.CompletedProcess]


def _default_runner(job: Job, argv: list[str]) -> subprocess.CompletedProcess:
    with open(job.log, "a") as log:
        log.write(f"\n--- scheduler: {job.name} @ {dt.datetime.now(dt.UTC).isoformat()} ---\n")
        log.flush()
        # argv comes from the static registry, never user input
        return subprocess.run(
            argv, stdout=log, stderr=subprocess.STDOUT, timeout=job.timeout_s, check=False
        )


@dataclass
class TickReport:
    ran: list[str] = field(default_factory=list)
    skipped_locked: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    health_triggered: bool = False
    triage_triggered: list[str] = field(default_factory=list)
    pace: int | None = None


def run_tick(
    *,
    now: dt.datetime,
    period_s: float,
    pace: int | None,
    jobs: Sequence[Job] = JOBS,
    runner: Runner | None = None,
) -> TickReport:
    """One scheduler tick: run every due job sequentially, record outcomes,
    and hand persistent failures to the error agent."""
    run = runner or _default_runner
    binary = _agents_binary()
    report = TickReport(pace=pace)
    for job in due_jobs(now, period_s, jobs):
        lock = acquire_lock(job.name)
        if lock is None:
            report.skipped_locked.append(job.name)
            continue
        try:
            argv = [binary, *job.args]
            if job.paced and pace is not None:
                argv += ["--pace", str(pace)]
            started = dt.datetime.now(dt.UTC)
            try:
                proc = run(job, argv)
                ok, returncode = proc.returncode == 0, proc.returncode
            except subprocess.TimeoutExpired:
                ok, returncode = False, -1
            duration = (dt.datetime.now(dt.UTC) - started).total_seconds()
            failures = record_outcome(job.name, ok=ok, returncode=returncode, duration_s=duration)
            report.ran.append(job.name)
            if ok:
                continue
            report.failed.append(job.name)
            logger.warning("job %s failed (rc=%s, %s consecutive)", job.name, returncode, failures)
            if failures >= HEALTH_AFTER_FAILURES and job.name != "ops_health":
                health = next(j for j in jobs if j.name == "ops_health")
                run(health, [binary, *health.args])
                report.health_triggered = True
            if failures >= TRIAGE_AFTER_FAILURES and triage_allowed(job.name):
                prompt = (
                    f"The scheduled job '{job.name}' has failed {failures} times in a row "
                    f"(last rc={returncode}; log: {job.log}). Diagnose with your tools — "
                    f"feed_health first — remediate within your allow-list, or escalate."
                )
                triage = Job(name="incident_triage", args=("ops", "run", "incident_triage", prompt),
                             log="/tmp/agents-ops.log", timeout_s=1800)
                run(triage, [binary, *triage.args])
                report.triage_triggered.append(job.name)
        finally:
            lock.unlink(missing_ok=True)
    return report


def status() -> dict[str, dict]:
    """Last outcome per job (for `agents schedule --status`)."""
    from sportsdata_agents.tools.ops import read_ops_state

    state = read_ops_state()
    runs = state.get("job_runs") or {}
    failures = state.get("job_failures") or {}
    def _schedule(job: Job) -> str:
        if job.interval_s is not None:
            return f"every {job.interval_s}s"
        assert job.at is not None
        days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        day = days[job.weekday] if job.weekday is not None else "daily"
        return f"{day} {job.at[0]:02d}:{job.at[1]:02d}"

    return {
        job.name: {
            "schedule": _schedule(job),
            "last_run": runs.get(job.name),
            "consecutive_failures": int(failures.get(job.name, 0)),
            "paced": job.paced,
        }
        for job in JOBS
    }


def render_json(value: object) -> str:
    return json.dumps(value, indent=2, default=str)
