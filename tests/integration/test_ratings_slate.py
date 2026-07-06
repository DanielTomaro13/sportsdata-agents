"""Book-independent fair prices: ratings from results, form from race form.

These run the REAL local engines package (installed alongside): the point is
that warehouse rows in → engine boards out → predictions recorded under the
ratings artifacts, with no book anchors anywhere in the path.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import (
    EventResult,
    ModelArtifact,
    OddsSnapshot,
    Prediction,
    RaceForm,
)
from sportsdata_agents.data.repository import TenantScope
from sportsdata_agents.quant.ratings import (
    _parse_result,
    parse_last_starts,
    record_ratings_slate,
)
from sportsdata_agents.quant.slate import _seed_for

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 7, 7, 0, 0, tzinfo=dt.UTC)
SCOPE = TenantScope("local", "local")
TEAMS = ("Broncos", "Storm", "Panthers", "Roosters")


def test_parse_result_shapes() -> None:
    assert _parse_result("Broncos v Storm", "24-12", "home") == ("Broncos", "Storm", 24, 12)
    assert _parse_result("Broncos vs Storm", "10-30", "away") == ("Broncos", "Storm", 10, 30)
    assert _parse_result("Broncos v Storm", "24-12", "away") is None  # contradicts grading
    assert _parse_result("no separator here", "24-12", "home") is None
    assert _parse_result("Broncos v Storm", "not-a-score", "home") is None


def test_parse_last_starts_decay_shape() -> None:
    runs = parse_last_starts("f2130", days_since_run=9)
    assert [r.position for r in runs] == [10, 2, 1, 3, 10]  # f=tail, 0=10th
    assert runs[0].age_days == 9.0 and runs[1].age_days == 23.0
    assert parse_last_starts("", 5) == []


def test_seed_for_sport_shapes() -> None:
    class Row:
        def __init__(self, market: str, selection: str, odds: float) -> None:
            self.market, self.selection, self.odds = market, selection, odds

    racing = _seed_for("racing", [Row("win", "1", 2.5), Row("win", "2", 3.0)])
    assert racing == {"win_odds": {"1": 2.5, "2": 3.0}}
    assert _seed_for("racing", [Row("win", "1", 2.5)]) is None  # one runner isn't a race
    mma = _seed_for("mma", [Row("h2h", "home", 1.7), Row("h2h", "away", 2.1)])
    assert mma == {"h2h": [1.7, 2.1]}
    tennis = _seed_for("tennis", [
        Row("h2h", "home", 1.8), Row("h2h", "away", 2.0),
        Row("total", "over 22.5", 1.9), Row("total", "under 22.5", 1.9),
    ])
    assert tennis is not None and "total_games" in tennis and "total" not in tennis


async def _seed_results(s: AsyncSession, n_rounds: int = 15) -> None:
    """A small league history with a clear pecking order: Broncos > Storm >
    Panthers > Roosters — enough scored results to clear MIN_RESULTS."""
    strengths = {"Broncos": 28, "Storm": 24, "Panthers": 20, "Roosters": 16}
    when = NOW - dt.timedelta(days=3)
    count = 0
    for rnd in range(n_rounds):
        for i, home in enumerate(TEAMS):
            for away in TEAMS[i + 1:]:
                hs, as_ = strengths[home] + 4, strengths[away]  # home edge
                s.add(EventResult(
                    provider="league", sport="rugby_league",
                    event_external_id=f"r{rnd}:{home}:{away}",
                    winning_selection="home" if hs > as_ else "away",
                    start_time=when - dt.timedelta(days=7 * rnd),
                    settled_at=when - dt.timedelta(days=7 * rnd),
                    meta={"event_name": f"{home} v {away}", "score": f"{hs}-{as_}"},
                ))
                count += 1
    assert count >= 40
    await s.commit()


async def test_ratings_slate_records_book_free_predictions(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        await _seed_results(s)
        # an upcoming fixture the ratings know both sides of — NO odds needed
        s.add(OddsSnapshot(
            captured_at=NOW, provider="sportsbet", book="Sportsbet",
            sport="rugby_league", event_external_id="SB-999",
            event_name="Broncos v Roosters", market="h2h", selection="home",
            odds=1.5, start_time=NOW + dt.timedelta(hours=20)))
        await s.commit()
    async with db_sessionmaker() as s:
        report = await record_ratings_slate(s, SCOPE, now=NOW)
    assert report.get("error") is None, report
    assert report["events"] >= 1 and report["recorded"] > 0, report
    async with db_sessionmaker() as s:
        artifact = (await s.execute(select(ModelArtifact).where(
            ModelArtifact.name == "engine-ratings:rugby_league"))).scalars().first()
        assert artifact is not None
        rows = (await s.execute(select(Prediction).where(
            Prediction.model_id == artifact.id))).scalars().all()
        assert rows, "ratings board recorded no predictions"
        h2h = {r.selection: float(r.prob) for r in rows if r.market == "h2h"}
        # the stronger side must price shorter — the whole point of an opinion
        assert h2h.get("home", 0) > h2h.get("away", 1), h2h
    # a second run inside the dedupe window records nothing new
    async with db_sessionmaker() as s:
        again = await record_ratings_slate(s, SCOPE, now=NOW + dt.timedelta(minutes=30))
    assert again["events"] == 0 and again["skipped_dedupe"] >= 1


async def test_form_slate_prices_a_race_from_form_alone(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runners = [
        {"number": 1, "name": "FAST CAR", "last_starts": "11212", "days_since_run": 9,
         "scratched": False},
        {"number": 2, "name": "MID PACK", "last_starts": "44554", "days_since_run": 12,
         "scratched": False},
        {"number": 3, "name": "BACKMARKER", "last_starts": "89907", "days_since_run": 20,
         "scratched": False},
        {"number": 4, "name": "SCRATCHED ONE", "last_starts": "1111", "days_since_run": 7,
         "scratched": True},
    ]
    async with db_sessionmaker() as s:
        s.add(RaceForm(provider="tab_racing", race_key="2026-07-07:G:QST:R9",
                       meeting_date="2026-07-07", race_type="G", venue_mnemonic="QST",
                       race_number=9, start_time=NOW + dt.timedelta(hours=1),
                       runners=runners, captured_at=NOW))
        await s.commit()
    async with db_sessionmaker() as s:
        report = await record_ratings_slate(s, SCOPE, now=NOW)
    assert report.get("error") is None, report
    assert report["events"] == 1 and report["recorded"] > 0, report
    async with db_sessionmaker() as s:
        artifact = (await s.execute(select(ModelArtifact).where(
            ModelArtifact.name == "engine-form:racing"))).scalars().first()
        assert artifact is not None
        rows = (await s.execute(select(Prediction).where(
            Prediction.model_id == artifact.id, Prediction.market == "win"))).scalars().all()
        probs = {r.selection: float(r.prob) for r in rows}
        # saddle numbers key the win rows (results settle on numbers), the
        # scratched runner is absent, and form order holds
        assert set(probs) == {"1", "2", "3"}, probs
        assert probs["1"] > probs["2"] > probs["3"], probs