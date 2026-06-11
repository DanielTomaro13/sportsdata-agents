"""Arb detection math — pure, fixture-driven (quant.arbitrage)."""

from __future__ import annotations

import pytest

from sportsdata_agents.quant.arbitrage import arbs_for_fixture

pytestmark = pytest.mark.unit

FIXTURE = "San Antonio Spurs v New York Knicks"


def _row(provider: str, book: str, selection: str, odds: float,
         event_name: str = FIXTURE) -> dict:
    return {"provider": provider, "book": book, "selection": selection,
            "odds": odds, "event_name": event_name}


def test_two_way_arb_with_orientation_flip() -> None:
    rows = [
        _row("tab", "TAB", "home", 2.10),
        _row("tab", "TAB", "away", 1.75),
        # sportsbet lists the teams REVERSED — its "home" is the fixture's away
        _row("sportsbet", "Sportsbet", "home", 2.12,
             event_name="New York Knicks v San Antonio Spurs"),
        _row("sportsbet", "Sportsbet", "away", 1.80,
             event_name="New York Knicks v San Antonio Spurs"),
    ]
    arbs = arbs_for_fixture(FIXTURE, "h2h", rows, threshold_pct=0.5)
    assert len(arbs) == 1
    arb = arbs[0]
    # best home = TAB 2.10; best away = sportsbet's (flipped) "home" 2.12
    legs = {leg["outcome"]: leg for leg in arb["legs"]}
    assert legs["home"]["book"] == "TAB" and legs["home"]["odds"] == 2.10
    assert legs["away"]["book"] == "Sportsbet" and legs["away"]["odds"] == 2.12
    inv = 1 / 2.10 + 1 / 2.12
    assert arb["margin_pct"] == pytest.approx((1 - inv) * 100, abs=0.01)
    # equalised stakes: shares sum to 1, payouts equal across legs
    shares = [leg["stake_share"] for leg in arb["legs"]]
    assert sum(shares) == pytest.approx(1.0, abs=1e-3)
    payouts = {leg["outcome"]: leg["stake_share"] * leg["odds"] for leg in arb["legs"]}
    assert max(payouts.values()) - min(payouts.values()) < 1e-2


def test_exchange_no_contract_folds_on_two_way_board() -> None:
    rows = [
        _row("tab", "TAB", "home", 1.95),
        _row("tab", "TAB", "away", 1.80),
        # kalshi names the teams; its NO contract IS the other side on a 2-way
        _row("kalshi", "Kalshi", "no san antonio", 2.20,
             event_name="San Antonio vs New York"),
        _row("kalshi", "Kalshi", "san antonio", 1.90,
             event_name="San Antonio vs New York"),
    ]
    arbs = arbs_for_fixture(FIXTURE, "h2h", rows, threshold_pct=0.5)
    assert len(arbs) == 1
    legs = {leg["outcome"]: leg for leg in arbs[0]["legs"]}
    assert legs["home"]["book"] == "TAB" and legs["home"]["odds"] == 1.95
    assert legs["away"]["book"] == "Kalshi" and legs["away"]["odds"] == 2.20
    assert legs["away"]["listed_as"] == "no san antonio"  # the folded NO is the leg
    inv = 1 / 1.95 + 1 / 2.20
    assert arbs[0]["margin_pct"] == pytest.approx((1 - inv) * 100, abs=0.01)


def test_three_way_frame_requires_the_draw() -> None:
    soccer = "Arsenal v Chelsea"
    rows = [
        _row("tab", "TAB", "home", 2.60, event_name=soccer),
        _row("tab", "TAB", "draw", 3.60, event_name=soccer),
        _row("tab", "TAB", "away", 3.10, event_name=soccer),
        _row("pinnacle", "Pinnacle", "home", 2.75, event_name=soccer),
        _row("pinnacle", "Pinnacle", "away", 3.40, event_name=soccer),
    ]
    # home 2.75 + away 3.40 alone would "arb" — but TAB's board proves a draw exists
    arbs = arbs_for_fixture(soccer, "h2h", rows, threshold_pct=0.5)
    inv = 1 / 2.75 + 1 / 3.60 + 1 / 3.40
    if inv < 1:
        assert all(len(a["legs"]) == 3 for a in arbs)
    else:
        assert arbs == []


def test_totals_combine_same_line_only() -> None:
    rows = [
        _row("tab", "TAB", "over 165.5", 2.05),
        _row("tab", "TAB", "under 165.5", 1.85),
        # a different line never pairs with 165.5
        _row("pinnacle", "Pinnacle", "over 166.5", 2.30),
        _row("pinnacle", "Pinnacle", "under 165.5", 2.02),
    ]
    arbs = arbs_for_fixture(FIXTURE, "total", rows, threshold_pct=0.5)
    assert len(arbs) == 1
    arb = arbs[0]
    assert arb["line"] == "165.5"
    legs = {leg["outcome"]: leg for leg in arb["legs"]}
    assert legs["over 165.5"]["odds"] == 2.05  # 166.5 over excluded despite better price
    assert legs["under 165.5"]["odds"] == 2.02


def test_single_book_boards_and_thresholds_do_not_fire() -> None:
    rows = [
        _row("tab", "TAB", "home", 2.30),
        _row("tab", "TAB", "away", 2.30),  # one book summing under 1 = capture artifact
    ]
    assert arbs_for_fixture(FIXTURE, "h2h", rows, threshold_pct=0.5) == []
    rows = [
        _row("tab", "TAB", "home", 1.95),
        _row("tab", "TAB", "away", 2.02),
        _row("pinnacle", "Pinnacle", "home", 2.02),
        _row("pinnacle", "Pinnacle", "away", 1.95),
    ]
    # best legs land on DIFFERENT books; margin ≈ 0.99% — below a 2% threshold
    assert arbs_for_fixture(FIXTURE, "h2h", rows, threshold_pct=2.0) == []
    found = arbs_for_fixture(FIXTURE, "h2h", rows, threshold_pct=0.5)
    assert len(found) == 1 and found[0]["margin_pct"] == pytest.approx(0.99, abs=0.01)
    assert {leg["book"] for leg in found[0]["legs"]} == {"TAB", "Pinnacle"}


def test_untranslatable_orientation_drops_the_book() -> None:
    rows = [
        _row("tab", "TAB", "home", 2.40),
        _row("tab", "TAB", "away", 2.40),
        # this book's name doesn't split — its sides can't be oriented
        _row("betr", "BetR", "home", 2.60, event_name="Spurs Knicks Special"),
        _row("betr", "BetR", "away", 2.60, event_name="Spurs Knicks Special"),
    ]
    arbs = arbs_for_fixture(FIXTURE, "h2h", rows, threshold_pct=0.5)
    assert arbs == []  # TAB alone is one book; BetR's legs dropped, no fake arb
