"""The sharp line: de-vig each low-margin source, average de-vigged probs across
whichever priced the game, and value the books against that fair."""

from __future__ import annotations

from sportsdata_agents.quant.sharp_line import (
    blend_sharp,
    book_value,
    devig,
    sharp_line,
)


def test_devig_removes_the_margin():
    # a book at 1.90/1.90 carries ~5.3% margin -> fair 50/50
    d = devig({"home": 1.90, "away": 1.90})
    assert abs(d["home"] - 0.5) < 1e-9
    assert abs(d["away"] - 0.5) < 1e-9


def test_devig_refuses_an_incomplete_or_bad_market():
    assert devig({"home": 1.90}) == {"home": 1.0}  # single-outcome still normalises
    assert devig({"home": 1.90, "away": 1.0}) == {}   # a <=1.0 price is a bug
    assert devig({}) == {}


def test_blend_averages_devigged_probs_across_present_sources():
    by_source = {
        "Betfair": {"home": 2.0, "away": 2.0},        # fair 50/50
        "Pinnacle": {"home": 1.5, "away": 3.0},       # fair ~66.7/33.3
    }
    b = blend_sharp(by_source)
    assert set(b["sources"]) == {"Betfair", "Pinnacle"} and b["n"] == 2
    # mean of (0.5, 0.667) = 0.583 home, renormalised (already sums to 1)
    assert abs(b["fair"]["home"] - (0.5 + 2 / 3) / 2) < 1e-6


def test_blend_uses_only_whichever_sharps_are_present():
    # AFL-style: only Betfair present
    b = blend_sharp({"Betfair": {"home": 1.8, "away": 2.2}})
    assert b["sources"] == ["Betfair"] and b["n"] == 1
    assert abs(sum(b["fair"].values()) - 1.0) < 1e-9


def test_book_value_flags_a_book_longer_than_the_sharps():
    fair = {"home": 0.5, "away": 0.5}
    by_book = {"Sportsbet": {"home": 2.20, "away": 1.80},
               "TAB": {"home": 2.10, "away": 1.85}}
    v = book_value(by_book, fair)
    # best home price 2.20 vs fair 0.5 -> +10% value, from Sportsbet
    assert v["home"]["best_book"] == "Sportsbet"
    assert v["home"]["best_odds"] == 2.20
    assert v["home"]["value_pct"] == 10.0
    assert v["home"]["fair_odds"] == 2.0
    # away best 1.85 vs fair 0.5 -> -7.5%
    assert v["away"]["value_pct"] == -7.5


def test_book_value_suppresses_deep_longshot_fairs():
    fair = {"home": 0.95, "away": 0.05}  # away is a 20/1 dog (fair 20.0 > cap)
    v = book_value({"Sportsbet": {"away": 26.0}}, fair)
    assert v["away"]["value_pct"] is None  # untrustworthy regime, no value call


def test_sharp_line_splits_sharps_from_books_end_to_end():
    quotes = {
        "Kalshi": {"home": 1.95, "away": 1.95},
        "Polymarket": {"home": 2.0, "away": 2.0},
        "Betfair": {"home": 2.02, "away": 1.98},
        "Sportsbet": {"home": 2.25, "away": 1.72},   # a book, not a sharp
        "TAB": {"home": 2.10, "away": 1.80},
    }
    r = sharp_line(quotes)
    assert set(r["sharp_sources"]) == {"Kalshi", "Polymarket", "Betfair"}
    assert r["book_count"] == 2
    # fair is ~50/50 (all three sharps near evens); Sportsbet home 2.25 is value
    assert abs(r["fair"]["home"] - 0.5) < 0.02
    assert r["value"]["home"]["best_book"] == "Sportsbet"
    assert r["value"]["home"]["value_pct"] > 0


def test_sharp_line_with_no_sharps_yields_no_fair():
    r = sharp_line({"Sportsbet": {"home": 2.0, "away": 2.0}})
    assert r["fair"] == {} and r["sharp_sources"] == [] and r["value"] == {}
