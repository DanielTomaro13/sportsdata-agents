"""Exchange premium scan + watch: book price vs de-vigged Betfair fair."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import Event, Fixture, OddsSnapshot, Subscription
from sportsdata_agents.operations.monitoring import run_watches
from sportsdata_agents.quant.arbitrage import scan_exchange_premium

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 7, 6, 9, 0, tzinfo=dt.UTC)


def _snap(provider: str, book: str, event_id: str, selection: str, odds: float,
          event_name: str, market: str = "h2h",
          matched: float | None = 50_000.0) -> OddsSnapshot:
    # Betfair rows carry the market's traded volume; the scans refuse to price
    # a fair off a near-untraded market, so seeds model a LIQUID one by default
    meta = {"total_matched": matched} if (provider == "betfair" and matched is not None) else {}
    return OddsSnapshot(
        captured_at=NOW - dt.timedelta(minutes=10), provider=provider, book=book,
        sport="tennis", event_external_id=event_id, event_name=event_name,
        market=market, selection=selection, odds=odds, meta=meta,
    )


async def _seed(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    fixture_id = uuid.uuid4()
    async with db_sessionmaker() as s:
        s.add(Fixture(id=fixture_id, sport="tennis", external_id="FX-1",
                      name="Alex De Minaur v Flavio Cobolli",
                      start_time=NOW + dt.timedelta(hours=3)))
        await s.flush()  # the FK-enforcing Postgres matrix needs the fixture first
        s.add(Event(fixture_id=fixture_id, provider="betfair", external_id="BF-1"))
        s.add(Event(fixture_id=fixture_id, provider="sportsbet", external_id="SB-1"))
        s.add(Event(fixture_id=fixture_id, provider="dabble", external_id="DB-1"))
        # Betfair back prices: 1.30 / 4.80 -> de-vig fair 0.7702 / 0.2086
        s.add(_snap("betfair", "Betfair", "BF-1", "home", 1.30,
                    "De Minaur v Cobolli"))
        s.add(_snap("betfair", "Betfair", "BF-1", "away", 4.80,
                    "De Minaur v Cobolli"))
        # Sportsbet pays 5.50 on Cobolli: 5.50 * 0.2086 - 1 = +14.7% premium
        s.add(_snap("sportsbet", "sportsbet", "SB-1", "home", 1.26,
                    "Alex De Minaur v Flavio Cobolli"))
        s.add(_snap("sportsbet", "sportsbet", "SB-1", "away", 5.50,
                    "Alex De Minaur v Flavio Cobolli"))
        # Dabble is inside fair on both sides — must NOT fire
        s.add(_snap("dabble", "Dabble", "DB-1", "home", 1.28,
                    "Alex De Minaur v Flavio Cobolli"))
        s.add(_snap("dabble", "Dabble", "DB-1", "away", 4.40,
                    "Alex De Minaur v Flavio Cobolli"))
        await s.commit()


async def test_scan_finds_the_premium_and_skips_fair_books(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_sessionmaker)
    async with db_sessionmaker() as s:
        found = await scan_exchange_premium(s, min_edge_pct=3.0, now=NOW)
    assert len(found) == 1, found
    hit = found[0]
    assert hit["book"] == "sportsbet" and hit["outcome"] == "away"
    assert hit["odds"] == 5.50
    # fair prob for away = (1/4.8) / (1/1.3 + 1/4.8); edge = 5.5 * fair - 1
    fair = (1 / 4.8) / (1 / 1.3 + 1 / 4.8)
    assert hit["edge_pct"] == pytest.approx(5.5 * fair * 100 - 100, abs=0.05)
    assert hit["exchange_fair_odds"] == pytest.approx(1 / fair, abs=0.01)


async def test_totals_devig_per_line_not_across_lines(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Review finding: two total lines under one (fixture, 'total') board were
    de-vigged TOGETHER (inv≈2.0), mangling every edge. Each line is its own
    two-way market."""
    fixture_id = uuid.uuid4()
    async with db_sessionmaker() as s:
        s.add(Fixture(id=fixture_id, sport="basketball", external_id="FX-T",
                      name="Hawks v Wolves", start_time=NOW + dt.timedelta(hours=3)))
        await s.flush()
        s.add(Event(fixture_id=fixture_id, provider="betfair", external_id="BF-T"))
        s.add(Event(fixture_id=fixture_id, provider="sportsbet", external_id="SB-T"))
        for sel, odds in (("over 165.5", 1.95), ("under 165.5", 1.95),
                          ("over 220.5", 6.0), ("under 220.5", 1.12)):
            s.add(_snap("betfair", "Betfair", "BF-T", sel, odds,
                        "Hawks v Wolves", market="total"))
        # book pays 2.30 on over 165.5: fair prob 0.5 -> +15% vs THIS line only
        s.add(_snap("sportsbet", "sportsbet", "SB-T", "over 165.5", 2.30,
                    "Hawks v Wolves", market="total"))
        await s.commit()
    async with db_sessionmaker() as s:
        found = await scan_exchange_premium(s, min_edge_pct=3.0, now=NOW)
    assert len(found) == 1, found
    # 1.95/1.95 de-vig -> fair prob exactly 0.5; the 220.5 line must not bleed in
    assert found[0]["edge_pct"] == pytest.approx(2.30 * 0.5 * 100 - 100, abs=0.05)


async def test_thin_or_junk_exchange_markets_never_price_a_fair(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Live false positive (Croatia v Israel basketball): a Betfair market with
    $16 matched and backs summing to 1.74 (stray offers nobody took) read as a
    +298% 'premium'. Thin markets and junk boards must never price a fair."""
    thin_id, junk_id = uuid.uuid4(), uuid.uuid4()
    async with db_sessionmaker() as s:
        for fid, ext in ((thin_id, "T"), (junk_id, "J")):
            s.add(Fixture(id=fid, sport="basketball", external_id=f"FX-{ext}",
                          name=f"Alpha {ext} v Beta {ext}",
                          start_time=NOW + dt.timedelta(hours=3)))
            await s.flush()
            s.add(Event(fixture_id=fid, provider="betfair", external_id=f"BF-{ext}"))
            s.add(Event(fixture_id=fid, provider="sportsbet", external_id=f"SB-{ext}"))
        # thin: a SANE board (inv 0.978 — inside the junk bound, so only the
        # liquidity gate can reject it) but only $16 ever traded
        s.add(_snap("betfair", "Betfair", "BF-T", "home", 1.30, "Alpha T v Beta T",
                    matched=16.30))
        s.add(_snap("betfair", "Betfair", "BF-T", "away", 4.80, "Alpha T v Beta T",
                    matched=16.30))
        s.add(_snap("sportsbet", "sportsbet", "SB-T", "away", 9.00, "Alpha T v Beta T"))
        # junk: liquid market but the listed backs sum to 1.74 — not a priced book
        s.add(_snap("betfair", "Betfair", "BF-J", "home", 1.03, "Alpha J v Beta J"))
        s.add(_snap("betfair", "Betfair", "BF-J", "away", 1.30, "Alpha J v Beta J"))
        s.add(_snap("sportsbet", "sportsbet", "SB-J", "away", 9.00, "Alpha J v Beta J"))
        await s.commit()
    async with db_sessionmaker() as s:
        found = await scan_exchange_premium(s, min_edge_pct=3.0, now=NOW)
    assert found == []


async def test_matched_money_rides_the_candidate(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_sessionmaker)
    async with db_sessionmaker() as s:
        found = await scan_exchange_premium(s, min_edge_pct=3.0, now=NOW)
    assert found and found[0]["exchange_matched"] == 50_000.0


async def test_thin_exchange_leg_never_arbs_and_liquid_leg_shows_matched(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A near-untraded exchange quote is one stray offer, not an arb leg; a
    liquid exchange leg carries its traded volume for the alert."""
    from sportsdata_agents.quant.arbitrage import scan_arbs

    thin_id, liquid_id = uuid.uuid4(), uuid.uuid4()
    async with db_sessionmaker() as s:
        for fid, ext in ((thin_id, "TA"), (liquid_id, "LA")):
            s.add(Fixture(id=fid, sport="tennis", external_id=f"FX-{ext}",
                          name=f"Gamma {ext} v Delta {ext}",
                          start_time=NOW + dt.timedelta(hours=3)))
            await s.flush()
            s.add(Event(fixture_id=fid, provider="betfair", external_id=f"BF-{ext}"))
            s.add(Event(fixture_id=fid, provider="sportsbet", external_id=f"SB-{ext}"))
        # best home (Betfair 2.10) + best away (book 2.10) = 4.76% margin — but
        # the exchange market has $12 matched: not an offer anyone can take
        for sel, bf_odds, sb_odds in (("home", 2.10, 1.60), ("away", 1.60, 2.10)):
            s.add(_snap("betfair", "Betfair", "BF-TA", sel, bf_odds,
                        "Gamma TA v Delta TA", matched=12.0))
            s.add(_snap("sportsbet", "sportsbet", "SB-TA", sel, sb_odds,
                        "Gamma TA v Delta TA"))
        # same shape with a liquid exchange market: a real arb, matched rides the leg
        for sel, bf_odds, sb_odds in (("home", 2.10, 1.60), ("away", 1.60, 2.10)):
            s.add(_snap("betfair", "Betfair", "BF-LA", sel, bf_odds,
                        "Gamma LA v Delta LA"))
            s.add(_snap("sportsbet", "sportsbet", "SB-LA", sel, sb_odds,
                        "Gamma LA v Delta LA"))
        await s.commit()
    async with db_sessionmaker() as s:
        arbs = await scan_arbs(s, threshold_pct=1.0, now=NOW)
    assert len(arbs) == 1, arbs
    assert arbs[0]["fixture"] == "Gamma LA v Delta LA"
    betfair_leg = next(leg for leg in arbs[0]["legs"] if leg["book"] == "Betfair")
    assert betfair_leg["matched"] == 50_000.0
    book_leg = next(leg for leg in arbs[0]["legs"] if leg["book"] == "sportsbet")
    assert "matched" not in book_leg  # books quote firm offers, no volume figure


async def test_watch_fires_once_and_dedupes(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_sessionmaker)
    async with db_sessionmaker() as s:
        s.add(Subscription(tenant_id="t", workspace_id="w", name="exch",
                           kind="exchange_value", channel="log",
                           params={"min_edge_pct": 3.0, "hours": 2.0}))
        await s.commit()
    pushed: list[str] = []

    async def pusher(sub: Subscription, message: str) -> bool:
        pushed.append(message)
        return True

    report = await run_watches(db_sessionmaker, pusher=pusher, now=NOW)
    assert report["alerts"] == 1
    assert "exchange premium" in pushed[0] and "sportsbet" in pushed[0]
    # the alert carries actionable sizing + honesty about the price's age:
    # kelly on the default $100 bankroll, and when the price was captured
    fair = (1 / 4.8) / (1 / 1.3 + 1 / 4.8)
    kelly = 100.0 * (fair * 5.5 - 1.0) / (5.5 - 1.0)
    assert f"kelly ${kelly:.2f} on $100" in pushed[0]
    assert "price seen 10m ago" in pushed[0]
    # unchanged condition -> deduped
    report = await run_watches(db_sessionmaker, pusher=pusher, now=NOW + dt.timedelta(minutes=5))
    assert report["alerts"] == 0
