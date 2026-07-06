"""Alert P&L scoreboard: printed-Kelly grading against recorded results."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import Alert, Event, EventResult, Fixture, Subscription
from sportsdata_agents.quant.scoreboard import alert_pnl, format_scoreboard

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 7, 6, 9, 0, tzinfo=dt.UTC)


async def _sub(s: AsyncSession) -> Subscription:
    sub = Subscription(tenant_id="t", workspace_id="w", name="rv",
                       kind="racing_value", channel="log", params={})
    s.add(sub)
    await s.flush()
    return sub


def _alert(sub: Subscription, kind: str, key: str, payload: dict) -> Alert:
    return Alert(subscription_id=sub.id, tenant_id="t", workspace_id="w",
                 kind=kind, dedupe_key=key, message=key, payload=payload,
                 created_at=NOW - dt.timedelta(days=1))


async def test_racing_settles_wins_losses_and_pending(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        sub = await _sub(s)
        # winner: staked $5 at 4.80 -> +$19
        s.add(_alert(sub, "racing_value", "a1", {
            "provider": "tab_racing", "event_external_id": "R-1",
            "runner_number": 7, "odds": 4.80, "kelly_stake": 5.0, "bankroll": 100.0}))
        # loser: staked $4 -> -$4
        s.add(_alert(sub, "racing_value", "a2", {
            "provider": "tab_racing", "event_external_id": "R-2",
            "runner_number": 3, "odds": 6.0, "kelly_stake": 4.0, "bankroll": 100.0}))
        # no result recorded yet -> pending, never guessed
        s.add(_alert(sub, "racing_value", "a3", {
            "provider": "tab_racing", "event_external_id": "R-3",
            "runner_number": 1, "odds": 3.0, "kelly_stake": 2.0, "bankroll": 100.0}))
        s.add(EventResult(provider="tab_racing", sport="horse_racing",
                          event_external_id="R-1", winning_selection="7"))
        s.add(EventResult(provider="tab_racing", sport="horse_racing",
                          event_external_id="R-2", winning_selection="5"))
        await s.commit()
    async with db_sessionmaker() as s:
        report = await alert_pnl(s, since=NOW - dt.timedelta(days=7), until=NOW)
    racing = report["racing"]
    assert racing["fired"] == 3 and racing["settled"] == 2 and racing["pending"] == 1
    assert racing["wins"] == 1
    assert racing["staked"] == pytest.approx(9.0)
    # +5*(4.8-1) - 4 = 19 - 4 = 15
    assert racing["pnl"] == pytest.approx(15.0)
    text = format_scoreboard(report)
    assert "P&L $+15.00" in text and "1 pending" in text


async def test_racing_settles_cross_book_through_the_fixture(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Results are recorded under ONE provider's race ids; an alert flagged on
    a DIFFERENT book must settle through the shared fixture — review finding:
    without this join, 4/5 of books' alerts stayed pending forever."""
    fixture_id = uuid.uuid4()
    async with db_sessionmaker() as s:
        sub = await _sub(s)
        s.add(Fixture(id=fixture_id, sport="horse_racing", external_id="FX-R",
                      name="Pakenham R5"))
        await s.flush()
        # the alert was flagged on SPORTSBET's race event…
        s.add(Event(fixture_id=fixture_id, provider="sportsbet_racing", external_id="SB-R5"))
        # …but the result was recorded under POINTSBET's race id
        s.add(Event(fixture_id=fixture_id, provider="pointsbet_racing", external_id="PB-999"))
        s.add(_alert(sub, "racing_value", "c1", {
            "provider": "sportsbet_racing", "event_external_id": "SB-R5",
            "runner_number": 4, "odds": 5.0, "kelly_stake": 3.0, "bankroll": 100.0}))
        s.add(EventResult(provider="pointsbet_racing", sport="horse_racing",
                          event_external_id="PB-999", winning_selection="4"))
        await s.commit()
    async with db_sessionmaker() as s:
        report = await alert_pnl(s, since=NOW - dt.timedelta(days=7), until=NOW)
    racing = report["racing"]
    assert racing["settled"] == 1 and racing["wins"] == 1
    assert racing["pnl"] == pytest.approx(3.0 * 4.0)  # 3 * (5.0 - 1)


async def test_league_winner_on_a_misjoined_fixture_stays_pending(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A fixture mis-merge carrying a 'home' winner must never grade a racing
    alert as a loss — pending is the honest answer."""
    fixture_id = uuid.uuid4()
    async with db_sessionmaker() as s:
        sub = await _sub(s)
        s.add(Fixture(id=fixture_id, sport="horse_racing", external_id="FX-M",
                      name="Somewhere R2"))
        await s.flush()
        s.add(Event(fixture_id=fixture_id, provider="tab_racing", external_id="TAB-R2"))
        s.add(Event(fixture_id=fixture_id, provider="espn", external_id="GAME-1"))
        s.add(_alert(sub, "racing_value", "d1", {
            "provider": "tab_racing", "event_external_id": "TAB-R2",
            "runner_number": 2, "odds": 4.0, "kelly_stake": 2.0, "bankroll": 100.0}))
        s.add(EventResult(provider="espn", sport="basketball",
                          event_external_id="GAME-1", winning_selection="home"))
        await s.commit()
    async with db_sessionmaker() as s:
        report = await alert_pnl(s, since=NOW - dt.timedelta(days=7), until=NOW)
    assert report["racing"]["settled"] == 0 and report["racing"]["pending"] == 1


async def test_arbs_report_locked_profit_only_when_still_takeable(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        sub = await _sub(s)
        s.add(_alert(sub, "arb", "b1", {
            "sum_inverse": 0.9524, "bankroll": 100.0,
            "outcome": {"still_arb": True, "margin_pct_after": 4.0}}))
        s.add(_alert(sub, "arb", "b2", {
            "sum_inverse": 0.98, "bankroll": 100.0,
            "outcome": {"still_arb": False, "margin_pct_after": -1.0}}))
        await s.commit()
    async with db_sessionmaker() as s:
        report = await alert_pnl(s, since=NOW - dt.timedelta(days=7), until=NOW)
    arbs = report["arbs"]
    assert arbs["fired"] == 2 and arbs["still_takeable"] == 1
    assert arbs["locked_profit"] == pytest.approx(100 * (1 / 0.9524 - 1), abs=0.01)
    # the vanished arb contributes NOTHING — not a loss, just untakeable
    text = format_scoreboard(report)
    assert "1 still takeable" in text
