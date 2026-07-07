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
        "runs": parse_comment_runs(
            " ".join([str(raw.get("formComment") or "")]
                     + [str(c.get("comment") or "") for c in raw.get("formComments") or []]),
            dt.datetime.now(dt.UTC)),
        "rating": raw.get("techFormRating") or raw.get("rating"),
        # the guide's words about the runner — shown on alerts, kept short
        "comment": (str(raw.get("formComment")
                        or next((c.get("comment") for c in raw.get("formComments") or []
                                 if c.get("comment")), "")
                        or "").strip()[:220] or None),
        "days_since_run": raw.get("daysSinceLastRun"),
        "runs_since_spell": raw.get("runsSinceSpell"),
        "best_time": raw.get("bestTime"),
        "age": raw.get("age"),
        "scratched": bool(raw.get("scratched") or raw.get("isScratched") or False),
    }


_MON_ALT = ("January|February|March|April|May|June|July|August|"
            "September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|"
            "Aug|Sept|Sep|Oct|Nov|Dec")
# one run per SENTENCE of form prose. Both TAB's formComments and Sportsbet's
# AU overviews speak the same grammar:
#   "3rd of 7 at Warwick Farm 2yo F Osc on June 24 over 1100m"
#   "ran second last of 7 at Yil All Weather on June 16"
#   "won by 2.5 len at Scone Red Crown May 31 over 900m"
_COMMENT_RUN = __import__("re").compile(
    r"(?:(?P<lastish>(?:second |third )?last of (?P<lfield>\d{1,2}))"
    r"|(?P<pos>\d{1,2})(?:st|nd|rd|th)? of (?P<field>\d{1,2})"
    r"|(?P<won>won) by [\w.\u00bd\u00bc ]{1,12}?len)"
    rf".{{0,90}}?(?:on )?(?P<mon>{_MON_ALT}) (?P<day>\d{{1,2}})\b",
    __import__("re").IGNORECASE)


def parse_comment_runs(comment: str, now: dt.datetime) -> list[dict[str, Any]]:
    """STRUCTURED runs from form-guide prose — position, field size and date
    per sentence. The printed dates carry no year: a month/day later than
    today reads as LAST year (form is always the recent past)."""
    runs: list[dict[str, Any]] = []
    for sentence in __import__("re").split(r"(?<=[.!?])\s+", str(comment or "")):
        match = _COMMENT_RUN.search(sentence)
        if not match:
            continue
        if match.group("won"):
            position, field = 1, 8  # field size unprinted on win lines — a
            # conservative stand-in; excluding wins would bias good horses DOWN
        elif match.group("lastish"):
            field = int(match.group("lfield"))
            back = match.group("lastish").split()[0].lower()
            position = field - {"second": 1, "third": 2}.get(back, 0)
        else:
            position, field = int(match.group("pos")), int(match.group("field"))
        month = _MONTHS.get(match.group("mon")[:3].lower())
        if month is None or not 1 <= position <= max(field, 1):
            continue
        try:
            ran = dt.datetime(now.year, month, int(match.group("day")), tzinfo=dt.UTC)
        except ValueError:
            continue
        if ran > now:
            ran = ran.replace(year=now.year - 1)
        age_days = (now - ran).total_seconds() / 86_400.0
        if age_days > 400:
            continue  # ancient or mis-parsed — form decay makes it noise anyway
        runs.append({"position": position, "field_size": max(field, position),
                     "age_days": round(age_days, 1)})
    return runs


_RECENT_START = __import__("re").compile(
    r"^(?P<pos>[A-Za-z]{1,3}|\d{1,2}) of (?P<field>\d{1,2})\s.+?"
    r"(?P<day>\d{1,2}) (?P<mon>[A-Za-z]{3,9}) (?P<yr>\d{2})\b")
_MONTHS = {m[:3].lower(): i + 1 for i, m in enumerate(
    ("January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"))}


def parse_recent_start(text: str, now: dt.datetime) -> dict[str, Any] | None:
    """One STRUCTURED past run from a racecard's recent-start line —
    "5 of 8 KARTEPE 14.94 lens 23 June 26 1400m ..." → position 5 of a
    9-runner field, aged from the printed date. Letters (F/PU/DNF) are
    non-finishes: scored as last of field."""
    match = _RECENT_START.match(str(text or "").strip())
    if not match:
        return None
    field = int(match.group("field"))  # "5 of 8" = fifth in a field of eight
    pos = match.group("pos")
    position = field if pos.isalpha() else min(int(pos), field)
    month = _MONTHS.get(match.group("mon")[:3].lower())
    if month is None:
        return None
    try:
        ran = dt.datetime(2000 + int(match.group("yr")), month,
                          int(match.group("day")), tzinfo=dt.UTC)
    except ValueError:
        return None
    age_days = (now - ran).total_seconds() / 86_400.0
    if age_days < 0:
        return None
    return {"position": position, "field_size": field, "age_days": round(age_days, 1)}


async def ingest_sportsbet_form(
    manager: Any,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """STRUCTURED run-by-run form from Sportsbet racecards (no auth needed):
    each selection's statistics.recentStarts lines carry position, field size
    and date — the exact inputs the form model wants, where TAB's guide only
    offers aggregates. Covers AU + international racing."""
    from sportsdata_agents.operations.ingestion.fetchers import fetch_sportsbet_races

    now = now or dt.datetime.now(dt.UTC)
    date = now.astimezone(_AEST).strftime("%Y-%m-%d")
    try:
        payload = await fetch_sportsbet_races(manager)
    except Exception as e:
        logger.warning("sportsbet form: racecards fetch failed: %s", e)
        return {"ok": False, "error": str(e)[:200]}
    sports = payload.get("sports") or {}
    meetings = payload.get("meetings") or {}
    type_code = {"horse_racing": "R", "greyhound_racing": "G", "harness_racing": "H"}
    rows: list[dict[str, Any]] = []
    for event in payload.get("events") or []:
        event_id = str(event.get("id", ""))
        venue = meetings.get(event_id) or event.get("competitionName") or ""
        race_no = event.get("raceNumber")
        if not venue or race_no is None:
            continue
        # scan every market: only some carry the runner statistics block, and
        # greyhound cards may carry none at all (a horses feature)
        by_number: dict[Any, dict[str, Any]] = {}
        for market in event.get("markets") or []:
            if not isinstance(market, dict):
                continue
            for sel in market.get("selections") or []:
                runner_number = sel.get("runnerNumber")
                if runner_number is None or runner_number in by_number:
                    continue
                stats = sel.get("statistics") or {}
                runs = [r for r in (parse_recent_start(line, now)
                                    for line in stats.get("recentStarts") or []) if r]
                if not runs:  # AU cards carry the same facts as prose
                    runs = parse_comment_runs(str(stats.get("overview") or ""), now)
                if runs:
                    by_number[runner_number] = {"number": runner_number,
                                                "name": sel.get("name"),
                                                "scratched": bool(sel.get("isOut")),
                                                "runs": runs}
        runners = list(by_number.values())
        if len(runners) < 3:
            continue
        start = event.get("startTime")
        start_dt = (dt.datetime.fromtimestamp(float(start), tz=dt.UTC)
                    if isinstance(start, int | float) else None)
        rows.append({"race_key": f"{date}:S:{venue}:R{race_no}",
                     "race_type": type_code.get(str(sports.get(event_id)), "R"),
                     "venue": str(venue)[:16], "number": int(race_no),
                     "start": start_dt, "runners": runners})
    stored = 0
    if rows:
        async with session_factory() as session:
            existing = {r.race_key for r in (await session.execute(
                select(RaceForm).where(RaceForm.meeting_date == date,
                                       RaceForm.provider == "sportsbet_racing")
            )).scalars().all()}
            for row in rows:
                if row["race_key"] in existing:
                    continue
                session.add(RaceForm(
                    provider="sportsbet_racing", race_key=row["race_key"],
                    meeting_date=date, race_type=row["race_type"],
                    venue_mnemonic=row["venue"], race_number=row["number"],
                    start_time=row["start"], runners=row["runners"], captured_at=now))
                stored += 1
            await session.commit()
    return {"ok": True, "races": len(rows), "stored": stored}


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
