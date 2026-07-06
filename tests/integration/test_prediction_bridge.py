"""Kalshi↔Polymarket bridge: pairing gates + disagreement math + watch wiring.

Every negative test here reproduces a REAL false positive from the first live
run against the warehouse — these are regression tests, not hypotheticals.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import OddsSnapshot, Subscription
from sportsdata_agents.operations.monitoring import run_watches
from sportsdata_agents.quant.prediction_bridge import scan_prediction_disagreements

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 7, 6, 9, 0, tzinfo=dt.UTC)


def _snap(provider: str, event_id: str, event_name: str, selection: str,
          odds: float, *, volume: float = 5000.0, sport: str = "politics",
          minutes_ago: float = 10.0) -> OddsSnapshot:
    book = "Kalshi" if provider == "kalshi" else "Polymarket"
    return OddsSnapshot(
        captured_at=NOW - dt.timedelta(minutes=minutes_ago), provider=provider,
        book=book, sport=sport, event_external_id=event_id, event_name=event_name,
        market="whatever", selection=selection, odds=odds,
        meta={"volume_24h": volume},
    )


async def test_same_question_disagreement_found(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Identical question, different wording: the higher-odds side is flagged
    with the other platform's price as fair."""
    async with db_sessionmaker() as s:
        s.add(_snap("kalshi", "KXVP", "2028 Democratic VP nominee",
                    "andy beshear", 14.0))
        s.add(_snap("polymarket", "pm-vp", "Democratic VP Nominee 2028",
                    "andy beshear", 24.0))
        # a second outcome agreeing closely — below the edge floor, must not fire
        s.add(_snap("kalshi", "KXVP", "2028 Democratic VP nominee",
                    "gretchen whitmer", 10.2))
        s.add(_snap("polymarket", "pm-vp", "Democratic VP Nominee 2028",
                    "gretchen whitmer", 10.6))
        await s.commit()
    async with db_sessionmaker() as s:
        found = await scan_prediction_disagreements(
            s, min_edge_pct=10.0, min_volume=1.0, now=NOW)
    assert len(found) == 1, found
    hit = found[0]
    assert hit["outcome"] == "andy beshear" and hit["back"] == "Polymarket"
    # edge = 24 * (1/14) - 1
    assert hit["edge_pct"] == pytest.approx((24.0 / 14.0 - 1.0) * 100.0, abs=0.1)
    assert hit["fair_odds"] == 14.0


async def test_year_mismatch_never_pairs(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Live false positive: Kalshi's '(2028)' Colorado Senate market paired
    with Polymarket's un-yeared one (the 2026 race) at +681% phantom edge."""
    async with db_sessionmaker() as s:
        s.add(_snap("kalshi", "KXSEN", "Colorado Senate winner? (2028)",
                    "republican party", 2.3))
        s.add(_snap("polymarket", "pm-sen", "Colorado Senate Election Winner",
                    "republican party", 18.0))
        await s.commit()
    async with db_sessionmaker() as s:
        found = await scan_prediction_disagreements(
            s, min_edge_pct=0.0, min_volume=1.0, now=NOW)
    assert found == []


async def test_weak_question_match_never_pairs(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Live false positives at Jaccard 0.6: presidential vs VP nominee, and
    OpenAI's IPO vs Anthropic's IPO — the 0.7 floor rejects both."""
    async with db_sessionmaker() as s:
        s.add(_snap("kalshi", "KXPRES", "2028 Republican presidential nominee",
                    "j.d. vance", 2.4))
        s.add(_snap("polymarket", "pm-vp2", "Republican VP Nominee 2028",
                    "j.d. vance", 16.7))
        s.add(_snap("kalshi", "KXIPO", "Which bank will lead OpenAI's IPO?",
                    "morgan stanley", 3.8, sport="tech"))
        s.add(_snap("polymarket", "pm-ipo", "Lead Bank in Anthropic's IPO?",
                    "morgan stanley", 2.2, sport="tech"))
        await s.commit()
    async with db_sessionmaker() as s:
        found = await scan_prediction_disagreements(
            s, min_edge_pct=0.0, min_volume=1.0, now=NOW)
    assert found == []


async def test_games_and_gates(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """'A vs B' questions are the fixture path's job (a team outcome can come
    from a spread market on one platform); longshots outside the prob band and
    zero-volume quotes are skipped too."""
    async with db_sessionmaker() as s:
        # vs-question: excluded even at perfect token match
        s.add(_snap("kalshi", "KXKBO", "LG Twins vs Samsung Lions",
                    "lg twins", 1.43, sport="baseball"))
        s.add(_snap("polymarket", "pm-kbo", "KBO: LG Twins vs. Samsung Lions",
                    "lg twins", 2.78, sport="baseball"))
        # longshot: fair side below the prob floor
        s.add(_snap("kalshi", "KXLONG", "Next Mars landing before 2031?",
                    "yes", 60.0, sport="science"))
        s.add(_snap("polymarket", "pm-long", "Mars landing before 2031?",
                    "yes", 200.0, sport="science"))
        # dead quote: no volume reported on the polymarket side
        s.add(_snap("kalshi", "KXPOPE", "Who will be the next Pope?",
                    "pietro parolin", 4.0))
        s.add(_snap("polymarket", "pm-pope", "Next Pope?",
                    "pietro parolin", 8.0, volume=0.0))
        await s.commit()
    async with db_sessionmaker() as s:
        found = await scan_prediction_disagreements(
            s, min_edge_pct=0.0, min_volume=1.0, now=NOW)
    assert found == []


async def test_duplicate_kalshi_events_dedupe(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Two Kalshi series variants matching one Polymarket question produce ONE
    signal per (question, outcome) — the strongest — not an alert per variant."""
    async with db_sessionmaker() as s:
        s.add(_snap("kalshi", "KXVP-A", "2028 Democratic VP nominee",
                    "wes moore", 21.0))
        s.add(_snap("kalshi", "KXVP-B", "Democratic VP nominee 2028",
                    "wes moore", 22.0))
        s.add(_snap("polymarket", "pm-vp", "Democratic VP Nominee 2028",
                    "wes moore", 30.0))
        await s.commit()
    async with db_sessionmaker() as s:
        found = await scan_prediction_disagreements(
            s, min_edge_pct=5.0, min_volume=1.0, now=NOW)
    assert len(found) == 1
    # the stronger disagreement (vs 21.0) survives
    assert found[0]["fair_odds"] == 21.0


async def test_watch_fires_with_plain_english_and_dedupes(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        # odds sit inside the watch's DEFAULT prob band (0.05-0.95) — the watch
        # is stricter than the scan's own default on deep longshots
        s.add(_snap("kalshi", "KXVP", "2028 Democratic VP nominee",
                    "andy beshear", 6.0))
        s.add(_snap("polymarket", "pm-vp", "Democratic VP Nominee 2028",
                    "andy beshear", 10.0))
        s.add(Subscription(tenant_id="t", workspace_id="w", name="pred",
                           kind="prediction_value", channel="log",
                           params={"min_edge_pct": 10.0, "min_volume": 1.0}))
        await s.commit()
    pushed: list[str] = []

    async def pusher(sub: Subscription, message: str) -> bool:
        pushed.append(message)
        return True

    report = await run_watches(db_sessionmaker, pusher=pusher, now=NOW)
    assert report["alerts"] == 1
    # plain English: the question and outcome, both platforms, no tickers
    assert "2028 Democratic VP nominee" in pushed[0]
    assert "andy beshear" in pushed[0]
    assert "Polymarket pays 10.00" in pushed[0]
    assert "KXVP" not in pushed[0]
    # unchanged condition -> deduped
    report = await run_watches(db_sessionmaker, pusher=pusher,
                               now=NOW + dt.timedelta(minutes=5))
    assert report["alerts"] == 0
