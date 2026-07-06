"""TAB form capture: upcoming races stored + refreshed, features normalized."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import RaceForm
from sportsdata_agents.operations.ingestion.form import ingest_tab_form, race_form_features

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 7, 6, 1, 0, tzinfo=dt.UTC)  # 11:00 AEST — racing morning


class FakeManager:
    def __init__(self) -> None:
        self.form_calls: list[str] = []

    async def call_tool(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "tab_racing_meetings":
            return {"meetings": [{
                "raceType": "R", "venueMnemonic": "RAND", "meetingName": "Randwick",
                "races": [
                    {"raceNumber": 1, "raceStartTime": "2026-07-06T02:30:00Z"},
                    {"raceNumber": 2, "raceStartTime": "2026-07-06T03:05:00Z"},
                    # already jumped — form is for the future
                    {"raceNumber": 0, "raceStartTime": "2026-07-06T00:30:00Z"},
                ],
            }]}
        if tool == "tab_racing_race_form":
            self.form_calls.append(f"{args['venueMnemonic']}:{args['raceNumber']}")
            return {"form": [  # the live key (response_hint said formData; live says form)
                {"runnerNumber": 1, "runnerName": "Boat Race", "barrierNumber": 4,
                 "handicapWeight": 58.5, "riderDriverName": "J McDonald",
                 "last20Starts": "x21341", "techFormRating": 98},
                {"runnerNumber": 2, "runnerName": "Scratchy", "isScratched": True},
            ]}
        raise AssertionError(tool)


async def test_form_captures_upcoming_races_and_skips_fresh(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    manager = FakeManager()
    report = await ingest_tab_form(manager, db_sessionmaker, now=NOW)
    assert report["ok"] is True and report["stored"] == 2
    assert manager.form_calls == ["RAND:1", "RAND:2"]  # jumped race never fetched
    async with db_sessionmaker() as s:
        rows = (await s.execute(select(RaceForm))).scalars().all()
    assert len(rows) == 2
    row = next(r for r in rows if r.race_number == 1)
    assert row.race_key == "2026-07-06:R:RAND:1"
    assert row.runners[0]["jockey"] == "J McDonald"
    # a re-run within the refresh window fetches NOTHING new
    manager.form_calls.clear()
    report = await ingest_tab_form(manager, db_sessionmaker, now=NOW + dt.timedelta(minutes=10))
    assert report["stored"] == 0 and report["fresh_skipped"] == 2
    assert manager.form_calls == []


def test_features_normalize_form_strings() -> None:
    row = RaceForm(race_key="k", meeting_date="2026-07-06", race_type="R",
                   venue_mnemonic="RAND", race_number=1, captured_at=NOW,
                   runners=[{"number": 1, "name": "Boat Race", "barrier": 4,
                             "weight": 58.5, "jockey": "J McDonald",
                             "last_starts": "x21341", "rating": 98,
                             "scratched": False}])
    feats = race_form_features(row)
    assert feats[0]["barrier"] == 4 and feats[0]["weight"] == 58.5
    # x21341 -> finishes 2,1,3,4,1 (x/0 markers excluded) -> avg 2.2
    assert feats[0]["avg_recent_finish"] == pytest.approx(2.2)
    assert feats[0]["starts_counted"] == 5
