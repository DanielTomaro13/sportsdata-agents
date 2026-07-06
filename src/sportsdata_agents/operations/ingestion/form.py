"""Official race form via TAB's authenticated tier — the racing ratings' real
inputs (M-Phase-B seam).

Market-only signals cannot see a wide barrier, a 3kg weight swing or a jockey
switch; the form guide can. This walks today's AU meetings (R/G/H), pulls the
form for races that have not yet jumped, and upserts one ``race_form`` row per
race with a trimmed per-runner dict. Needs TAB_CLIENT_ID/TAB_CLIENT_SECRET in
the env — without them TAB serves the anonymous tier and the form route 401s;
the job then reports zero rows rather than failing the conductor tick.

``race_form_features`` normalizes a stored row into the flat numbers a ratings
model consumes — the seam Phase B's racing fits plug into.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import RaceForm

logger = logging.getLogger(__name__)

_AEST = dt.timezone(dt.timedelta(hours=10))
FORM_RACES_PER_RUN = 30  # form guides are chunky; the cron re-runs anyway
_REFRESH_AFTER = dt.timedelta(hours=6)  # scratchings/jockey switches drift


def _trim_runner(raw: dict[str, Any]) -> dict[str, Any]:
    """The ratings-relevant slice of a form-guide runner (the raw guide is KBs
    of prose per runner)."""
    return {
        "number": raw.get("runnerNumber"),
        "name": raw.get("runnerName"),
        # dogs: the box IS the runner number; gallops carry an explicit barrier
        "barrier": raw.get("barrierNumber") or raw.get("barrier"),
        "weight": raw.get("handicapWeight") or raw.get("weight"),
        "jockey": raw.get("riderDriverName") or raw.get("rider") or raw.get("driver"),
        "trainer": raw.get("trainerName"),
        "last_starts": raw.get("last20Starts") or raw.get("lastStarts"),
        "rating": raw.get("techFormRating") or raw.get("rating"),
        "days_since_run": raw.get("daysSinceLastRun"),
        "runs_since_spell": raw.get("runsSinceSpell"),
        "best_time": raw.get("bestTime"),
        "age": raw.get("age"),
        "scratched": bool(raw.get("scratched") or raw.get("isScratched") or False),
    }


async def ingest_tab_form(
    manager: Any,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    max_races: int = FORM_RACES_PER_RUN,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.UTC)
    date = now.astimezone(_AEST).strftime("%Y-%m-%d")  # TAB dates are AU-local
    # TAB requires an explicit jurisdiction on every info-service call
    jurisdiction = os.environ.get("SPORTSDATA_AGENTS_TAB_JURISDICTION", "NSW")
    try:
        meetings = await manager.call_tool("tab_racing_meetings", {
            "date": date, "jurisdiction": jurisdiction})
    except Exception as e:
        logger.warning("tab form: meetings fetch failed: %s", e)
        return {"ok": False, "error": str(e)[:200]}

    # upcoming races, soonest first — the next race's form matters most
    wanted: list[tuple[str, str, str, str, int]] = []
    for meeting in meetings.get("meetings", []) or []:
        race_type = str(meeting.get("raceType") or "")[:1]
        venue = str(meeting.get("venueMnemonic") or "")
        if not race_type or not venue:
            continue
        for race in meeting.get("races", []) or []:
            start = str(race.get("raceStartTime") or "")
            if not start or start <= now.isoformat():
                continue  # jumped — form is for the FUTURE races
            wanted.append((start, date, race_type, venue, int(race.get("raceNumber", 0))))
    wanted.sort()

    # READ (short), FETCH (slow, no session held — the warehouse has one
    # writer and the cron writes constantly), then WRITE (one short txn)
    fresh_cutoff = now - _REFRESH_AFTER
    async with session_factory() as session:
        fresh_keys = {
            row.race_key
            for row in (await session.execute(
                select(RaceForm).where(RaceForm.meeting_date == date)
            )).scalars().all()
            if row.captured_at.replace(tzinfo=dt.UTC) > fresh_cutoff
        }

    fetched: list[tuple[str, str, str, str, int, str, list[dict[str, Any]]]] = []
    skipped = 0
    for start, d, race_type, venue, number in wanted:
        if len(fetched) >= max_races:
            break
        key = f"{d}:{race_type}:{venue}:{number}"
        if key in fresh_keys:
            skipped += 1
            continue
        try:
            guide = await manager.call_tool("tab_racing_race_form", {
                "date": d, "raceType": race_type,
                "venueMnemonic": venue, "raceNumber": number,
                "jurisdiction": jurisdiction,
            })
        except Exception as e:
            logger.info("tab form %s failed: %s", key, str(e)[:120])
            continue
        raw_runners = guide.get("form") or guide.get("formData") or []
        runners = [_trim_runner(r) for r in raw_runners]
        if runners:
            fetched.append((key, d, race_type, venue, number, start, runners))

    stored = 0
    if fetched:
        async with session_factory() as session:
            existing = {
                row.race_key: row
                for row in (await session.execute(
                    select(RaceForm).where(
                        RaceForm.race_key.in_([f[0] for f in fetched]))
                )).scalars().all()
            }
            for key, d, race_type, venue, number, start, runners in fetched:
                row = existing.get(key)
                if row is None:
                    session.add(RaceForm(
                        race_key=key, meeting_date=d, race_type=race_type,
                        venue_mnemonic=venue, race_number=number,
                        start_time=dt.datetime.fromisoformat(start.replace("Z", "+00:00")),
                        runners=runners, captured_at=now))
                else:
                    row.runners = runners
                    row.captured_at = now
                stored += 1
            await session.commit()
    return {"ok": True, "stored": stored, "fresh_skipped": skipped,
            "upcoming": len(wanted), "date": date}


def race_form_features(row: RaceForm) -> list[dict[str, Any]]:
    """One flat feature dict per runner — the ratings model's inputs. Form
    strings parse to a recent-finish average (x/0 = unplaced/spell markers
    excluded); missing values stay None rather than fabricated."""
    out: list[dict[str, Any]] = []
    for runner in row.runners or []:
        finishes: list[int] = []
        for ch in str(runner.get("last_starts") or ""):
            if ch.isdigit() and ch != "0":
                finishes.append(int(ch))
        out.append({
            "number": runner.get("number"),
            "name": runner.get("name"),
            "barrier": runner.get("barrier"),
            "weight": runner.get("weight"),
            "jockey": runner.get("jockey"),
            "rating": runner.get("rating"),
            "avg_recent_finish": round(sum(finishes) / len(finishes), 2) if finishes else None,
            "starts_counted": len(finishes),
            "scratched": bool(runner.get("scratched")),
        })
    return out
