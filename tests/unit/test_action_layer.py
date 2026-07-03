"""Action layer + slip advisories: ranking maths, redundancy, cash-out, tools."""

from __future__ import annotations

import pytest

from sportsdata_agents.quant.action import rank_value_board
from sportsdata_agents.quant.slips import cash_out_value, redundant_legs

pytestmark = pytest.mark.unit


def test_cash_out_value_and_validation() -> None:
    result = cash_out_value(0.6, 100.0, margin=0.05)
    assert result["fair_value"] == pytest.approx(60.0)
    assert result["shaded_value"] == pytest.approx(57.0)
    for bad in ((1.2, 100.0, 0.0), (0.5, -1.0, 0.0), (0.5, 100.0, 1.5)):
        with pytest.raises(ValueError):
            cash_out_value(*bad)


def test_redundant_legs_duplicate_opposed_and_exemption() -> None:
    legs = [
        {"market": "h2h", "selection": "home"},
        {"market": "h2h", "selection": "home"},        # duplicate
        {"market": "h2h", "selection": "away"},        # opposed to both
        {"market": "total", "selection": "over", "line": 44.5},  # different market: fine
        {"market": "place", "selection": "A", "line": 3.0, "single_winner": False},
        {"market": "place", "selection": "B", "line": 3.0, "single_winner": False},  # multi-winner: fine
    ]
    reasons = sorted(f["reason"] for f in redundant_legs(legs))
    assert reasons == ["duplicate", "opposed", "opposed"]


def test_value_board_ranks_and_flags_correlation() -> None:
    candidates = [
        {"edge_pct": 5.0, "std_error": 0.003, "model_prob": 0.55, "odds": 2.0,
         "age_minutes": 0.0, "event_external_id": "E1"},          # confident + fresh
        {"edge_pct": 8.0, "age_minutes": 60.0, "event_external_id": "E2"},  # big but stale, no error bar
        {"edge_pct": 4.0, "std_error": 0.003, "model_prob": 0.42, "odds": 2.5,
         "age_minutes": 0.0, "event_external_id": "E1"},          # same event as first
        {"edge_pct": -2.0, "event_external_id": "E3"},            # negative: dropped
    ]
    result = rank_value_board(candidates, freshness_half_life_minutes=15.0)
    board = result["board"]
    assert len(board) == 3
    assert board[0]["event_external_id"] == "E1" and board[0]["edge_pct"] == 5.0
    stale = next(r for r in board if r["event_external_id"] == "E2")
    assert stale["freshness"] == pytest.approx(0.0625, abs=1e-4)  # 4 half-lives
    assert stale["confidence"] == 0.5  # unknown certainty is not full certainty
    assert result["correlated_exposure"][0]["event_external_id"] == "E1"
    assert result["correlated_exposure"][0]["candidates"] == 2
    with pytest.raises(ValueError):
        rank_value_board([], top=0)


def test_advisory_tools_are_registered() -> None:
    from sportsdata_agents.tools.registry import NATIVE_TOOLS

    for name in ("cash_out_estimate", "slip_redundancy", "value_board"):
        assert name in NATIVE_TOOLS


def test_warehouse_key_mapping() -> None:
    from sportsdata_agents.tools.quant import _warehouse_key

    assert _warehouse_key("h2h", "home", None) == ("2way", "home")
    assert _warehouse_key("line", "away", 18.5) == ("spread", "away +18.5")
    assert _warehouse_key("line", "home", -15.5) == ("spread", "home -15.5")
    assert _warehouse_key("total", "over", 186.5) == ("total", "over 186.5")
    assert _warehouse_key("win", "Gossamer Glow", None) == ("win", "Gossamer Glow")
    assert _warehouse_key("h2h_3way", "draw", None) is None  # no stable convention: skip
    assert _warehouse_key("team_total_home", "over", 90.5) is None


def test_devig_curve_vs_proportional_on_odds_on_quotes() -> None:
    from sportsdata_agents.quant.devig import (
        OverroundCurve,
        piecewise_fair_probabilities,
        proportional_fair_probabilities,
    )

    odds = {"fav": 1.07, "second": 8.0, "third": 15.0, "long": 34.0}
    proportional = proportional_fair_probabilities(odds)
    piecewise = piecewise_fair_probabilities(odds)
    assert sum(proportional.values()) == pytest.approx(1.0)
    assert sum(piecewise.values()) == pytest.approx(1.0)
    # the curve refuses to strip impossible margin off the odds-on quote...
    assert piecewise["fav"] > proportional["fav"] + 0.03
    # ...and takes proportionally more off the longshot
    assert piecewise["long"] < proportional["long"]

    with pytest.raises(ValueError, match="incomplete or arbed"):
        proportional_fair_probabilities({"a": 3.0, "b": 3.5})
    with pytest.raises(ValueError):
        OverroundCurve(max_margin=0.5, high_break=0.8)  # non-monotone shape


def test_harville_place_probabilities() -> None:
    from sportsdata_agents.quant.racing_place import harville_place_probabilities

    win = {"A": 0.4, "B": 0.3, "C": 0.2, "D": 0.1}
    top2 = harville_place_probabilities(win, 2)
    top3 = harville_place_probabilities(win, 3)
    assert sum(top2.values()) == pytest.approx(2.0, abs=1e-12)
    assert sum(top3.values()) == pytest.approx(3.0, abs=1e-12)
    assert top3["A"] > top2["A"] > win["A"] / sum(win.values())
    assert harville_place_probabilities({"A": 0.6, "B": 0.4}, 2) == {"A": 1.0, "B": 1.0}
    with pytest.raises(ValueError):
        harville_place_probabilities({"A": 0.5, "B": -0.1}, 2)
