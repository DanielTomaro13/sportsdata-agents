"""Shadow account: the journal joins the alert stream honestly — matched
bets audited, unmatched ones reported as own picks, never dropped."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import Alert, Subscription
from sportsdata_agents.quant.shadow_account import (
    format_shadow_report,
    parse_bets,
    shadow_report,
)

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 7, 6, 9, 0, tzinfo=dt.UTC)


def _journal(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "bets.csv"
    p.write_text(text.strip() + "\n", encoding="utf-8")
    return p


def test_parse_handles_bookmaker_style_headers(tmp_path: Path) -> None:
    path = _journal(tmp_path, """
placed_at,event,selection,odds,stake,result,return
2026-07-05T08:30:00+00:00,Ascot Park R4,Homebush Warden,11.0,10,won,110
2026-07-05T09:00:00+00:00,Sale R10,Aventis,4.80,5,lost,0
2026-07-05T09:30:00+00:00,Sandown R1,Blue Shield,1.76,20,void,20
""")
    bets = parse_bets(path)
    assert [b.result for b in bets] == ["win", "loss", "void"]
    assert bets[0].pnl == 100.0     # returned - stake
    assert bets[1].pnl == -5.0
    assert bets[2].pnl == 0.0       # void: stake back, no information
    assert bets[0].placed_at is not None and bets[0].placed_at.tzinfo


def test_parse_handles_hand_kept_headers_and_derives_results(tmp_path: Path) -> None:
    path = _journal(tmp_path, """
Date,Race,Runner,Price,Amount,Status
05/07/2026 18:30,Riccarton R4,Ogun,9.00,25,WON
05/07/2026 19:00,Riccarton R5,Some Roughie,21.0,5,LOST
""")
    bets = parse_bets(path)
    assert bets[0].result == "win" and bets[0].pnl == 25 * 8.0
    assert bets[1].result == "loss" and bets[1].pnl == -5.0


def test_parse_rejects_a_file_with_no_selection_column(tmp_path: Path) -> None:
    path = _journal(tmp_path, "a,b,c\n1,2,3")
    with pytest.raises(ValueError, match="selection/odds"):
        parse_bets(path)


async def _seed_alert(s: AsyncSession, *, kind: str, message: str,
                      payload: dict, created: dt.datetime) -> None:
    sub = Subscription(tenant_id="t", workspace_id="w", name=f"s-{kind}",
                       kind=kind, channel="log", params={})
    s.add(sub)
    await s.flush()
    s.add(Alert(subscription_id=sub.id, tenant_id="t", workspace_id="w",
                kind=kind, dedupe_key=message[:40], message=message,
                payload=payload, created_at=created))


async def test_bets_match_alerts_and_audit_price_and_kelly(
    db_sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path,
) -> None:
    async with db_sessionmaker() as s:
        await _seed_alert(
            s, kind="bsp_value",
            message="Exchange value from form — Riccarton R4 / **Ogun**: back at 9.00",
            payload={"runners": [{"number": "7", "runner": "Ogun", "back": 9.0,
                                  "kelly_stake": 10.0}]},
            created=NOW - dt.timedelta(hours=2))
        await _seed_alert(
            s, kind="racing_value",
            message="Racing Value — Sale R10 / Aventis at 4.80",
            payload={"odds": 4.8, "kelly_stake": 4.0},
            created=NOW - dt.timedelta(hours=1))
        await s.commit()

    journal = _journal(tmp_path, f"""
placed_at,event,selection,odds,stake,result,return
{(NOW - dt.timedelta(hours=1)).isoformat()},Riccarton R4,Ogun,8.50,20,won,170
{NOW.isoformat()},Sale R10,Aventis,5.00,2,lost,0
{NOW.isoformat()},Somewhere R1,My Own Roughie,31.0,5,lost,0
""")
    bets = parse_bets(journal)
    async with db_sessionmaker() as s:
        report = await shadow_report(s, bets)

    assert report["bets"] == 3
    assert report["matched_to_alerts"] == 2
    assert report["own_picks"] == 1
    assert report["by_kind"]["bsp_value"]["bets"] == 1
    assert report["by_kind"]["racing_value"]["bets"] == 1
    assert report["own"]["bets"] == 1 and report["own"]["pnl"] == -5.0
    # price audit: bet 8.50 vs quote 9.00 (worse), 5.00 vs 4.80 (better)
    price = report["price_vs_alert"]
    assert price["n"] == 2
    assert price["beat_quote_share"] == 0.5
    # stake audit: 20 vs kelly 10 (2x) and 2 vs 4 (0.5x) -> mean 1.25x
    assert report["stake_vs_kelly"] == {"n": 2, "mean_ratio": 1.25}
    # the counterfactual scoreboard rides along for the same window
    assert report["counterfactual"] is not None
    text = format_shadow_report(report)
    assert "2 matched to alerts" in text and "own picks" in text


async def test_alert_after_the_bet_never_matches(
    db_sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path,
) -> None:
    async with db_sessionmaker() as s:
        await _seed_alert(
            s, kind="bsp_value", message="**Hindsight Hero** back at 6.00",
            payload={"runners": [{"back": 6.0}]},
            created=NOW + dt.timedelta(hours=3))  # fired AFTER the bet
        await s.commit()
    journal = _journal(tmp_path, f"""
placed_at,event,selection,odds,stake,result,return
{NOW.isoformat()},Anywhere R1,Hindsight Hero,6.0,10,won,60
""")
    async with db_sessionmaker() as s:
        report = await shadow_report(s, parse_bets(journal))
    assert report["matched_to_alerts"] == 0  # you can't have bet an alert
    assert report["own_picks"] == 1          # that didn't exist yet
