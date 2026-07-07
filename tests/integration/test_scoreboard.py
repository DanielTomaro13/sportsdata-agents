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


async def test_prediction_and_thin_racing_tuning_suggestion(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """prediction_value settles against a Kalshi/Polymarket resolution, and a
    losing thin-market racing streak produces a tuning suggestion."""
    async with db_sessionmaker() as s:
        sub = await _sub(s)
        # a prediction alert: backed Kalshi 'goldman sachs', which resolved YES
        s.add(_alert(sub, "prediction_value", "p1", {
            "back": "kalshi", "kalshi_event": "KXIPO",
            "outcome": "goldman sachs", "back_odds": 2.5, "kelly_stake": 4.0}))
        s.add(EventResult(provider="kalshi", sport="prediction",
                          event_external_id="KXIPO", winning_selection="goldman sachs"))
        # 6 losing thin racing bets to trip the min_matched suggestion
        for i in range(6):
            s.add(_alert(sub, "racing_value", f"t{i}", {
                "provider": "tab_racing", "event_external_id": f"TR-{i}",
                "runner_number": 1, "odds": 3.0, "kelly_stake": 2.0,
                "exchange_matched": 200}))
            s.add(EventResult(provider="tab_racing", sport="horse_racing",
                              event_external_id=f"TR-{i}", winning_selection="9"))
        await s.commit()
    async with db_sessionmaker() as s:
        report = await alert_pnl(s, since=NOW - dt.timedelta(days=7), until=NOW)
    assert report["value"]["settled"] == 1 and report["value"]["wins"] == 1
    assert report["value"]["pnl"] == pytest.approx(4.0 * 1.5)  # 4 * (2.5 - 1)
    assert any("min_matched" in tip for tip in report["suggestions"]), report["suggestions"]


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
    # credited at the RE-MEASURED margin (4.0%), not the fire-time 5.0%
    assert arbs["locked_profit"] == pytest.approx(4.0, abs=0.01)
    # the vanished arb contributes NOTHING — not a loss, just untakeable
    text = format_scoreboard(report)
    assert "1 still takeable" in text


async def test_model_value_settles_totals_and_lines_against_scores(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """model_value/value grade h2h, totals and lines from the result SCORE at
    a flat $1, frame-translated onto the fixture, with closing-line value."""
    from sportsdata_agents.data.models import Price

    async with db_sessionmaker() as s:
        sub = await _sub(s)
        fx = Fixture(sport="baseball", external_id="fx-1",
                     name="Miami Marlins v Seattle Mariners",
                     start_time=NOW - dt.timedelta(days=1, hours=-2))
        s.add(fx)
        await s.flush()
        s.add(Event(fixture_id=fx.id, provider="unibet", external_id="U-9"))
        # result lists the teams the OTHER way round: score must swap frames
        s.add(Event(fixture_id=fx.id, provider="mlb_api", external_id="M-9"))
        s.add(EventResult(provider="mlb_api", sport="baseball",
                          event_external_id="M-9", winning_selection="home",
                          meta={"event_name": "Seattle Mariners v Miami Marlins",
                                "score": "5-3"}))
        # fixture frame: Marlins 3, Mariners 5 -> total 8, home margin -2
        # over 7.5 at 1.86: WIN (+0.86 flat)
        s.add(_alert(sub, "model_value", "mv1", {
            "market": "total", "sport": "baseball", "event_key": str(fx.id),
            "book": "Unibet", "event_external_id": "U-9", "edge_pct": 7.0,
            "candidates": [{"market": "total", "selection": "over", "line": 7.5,
                            "odds": 1.86, "model_prob": 0.58, "book": "Unibet",
                            "event_id": "U-9"}]}))
        # home -1.5 at 1.90: LOSS (-1.00) — home lost by 2
        s.add(_alert(sub, "value", "v1", {
            "prob": 0.6, "provider": "Unibet", "book": "Unibet",
            "event_external_id": "U-9", "market": "line",
            "selection": "home -1.5", "odds": 1.90, "edge_pct": 14.0}))
        # closing price for the total: shortened to 1.80 after the alert
        s.add(Price(changed_at=fx.start_time - dt.timedelta(minutes=5),
                    provider="unibet", book="Unibet", sport="baseball",
                    event_external_id="U-9", market="total",
                    selection="over 7.5", odds=1.80))
        await s.commit()
    async with db_sessionmaker() as s:
        report = await alert_pnl(s, since=NOW - dt.timedelta(days=7), until=NOW)
    mv = report["flat"]["model_value"]
    assert mv["fired"] == 1 and mv["settled"] == 1 and mv["wins"] == 1
    assert mv["pnl"] == pytest.approx(0.86)
    # CLV: alerted 1.86 vs closing 1.80 -> +3.33%
    assert mv["clv_n"] == 1
    assert mv["clv_mean_pct"] == pytest.approx(3.33, abs=0.01)
    v = report["flat"]["value"]
    assert v["fired"] == 1 and v["settled"] == 1 and v["wins"] == 0
    assert v["pnl"] == pytest.approx(-1.0)
    text = format_scoreboard(report)
    assert "Model value (calibrated)" in text and "CLV" in text
