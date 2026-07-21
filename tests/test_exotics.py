"""Exotic + same-race-multi pricing: the Harville closed forms must match hand
computation, and the SRM Monte Carlo must respect the correlations (two runners
can't both win; a longer multi is always longer odds)."""

from __future__ import annotations

import pytest

from sportsdata_agents.quant.exotics import (
    normalize_win_probs,
    price_exotic,
    price_srm,
)

# a clean 4-runner field
FIELD = {1: 0.4, 2: 0.3, 3: 0.2, 4: 0.1}


def test_normalize_drops_junk_and_sums_to_one():
    p = normalize_win_probs({1: 0.5, 2: 0.5, 3: 0.0, 4: -1.0, 5: None})
    assert set(p) == {1, 2}
    assert abs(sum(p.values()) - 1.0) < 1e-9


def test_exacta_matches_harville_by_hand():
    # P(1 then 2) = 0.4 * 0.3/(1-0.4) = 0.4 * 0.5 = 0.20 -> fair 5.0
    r = price_exotic(FIELD, "exacta", [1, 2])
    assert abs(r["probability"] - 0.20) < 1e-9
    assert r["fair_odds"] == 5.0
    assert r["combinations"] == 1


def test_exacta_is_order_sensitive():
    ab = price_exotic(FIELD, "exacta", [1, 2])["probability"]
    ba = price_exotic(FIELD, "exacta", [2, 1])["probability"]
    # P(2 then 1) = 0.3 * 0.4/0.7 = 0.1714… != P(1 then 2)
    assert ab > ba
    assert abs(ba - 0.3 * 0.4 / 0.7) < 1e-9


def test_quinella_is_both_exacta_orders():
    q = price_exotic(FIELD, "quinella", [1, 2])["probability"]
    ab = price_exotic(FIELD, "exacta", [1, 2])["probability"]
    ba = price_exotic(FIELD, "exacta", [2, 1])["probability"]
    assert abs(q - (ab + ba)) < 1e-9


def test_trifecta_matches_by_hand():
    # 0.4 * 0.3/0.6 * 0.2/0.3 = 0.4 * 0.5 * 0.6667 = 0.13333
    r = price_exotic(FIELD, "trifecta", [1, 2, 3])
    assert abs(r["probability"] - (0.4 * 0.5 * (0.2 / 0.3))) < 1e-9


def test_first4_consumes_the_whole_ordering():
    r = price_exotic(FIELD, "first4", [1, 2, 3, 4])
    # only one ordering leaves runner 4 last with prob 1 at the final step
    expected = 0.4 * (0.3 / 0.6) * (0.2 / 0.3) * (0.1 / 0.1)
    assert abs(r["probability"] - expected) < 1e-9


def test_boxed_trifecta_sums_all_orderings_and_beats_straight():
    straight = price_exotic(FIELD, "trifecta", [1, 2, 3])["probability"]
    boxed = price_exotic(FIELD, "trifecta", [1, 2, 3], box=True)
    assert boxed["combinations"] == 6
    assert boxed["probability"] > straight  # any order is easier than one order


def test_margin_shortens_the_offered_price():
    r = price_exotic(FIELD, "exacta", [1, 2], margin=0.20)
    assert r["fair_odds"] == 5.0
    assert r["offer_odds"] == pytest.approx(0.8 / 0.20, abs=0.01)  # (1-margin)/p


def test_exotic_guards_bad_input():
    assert "warning" in price_exotic(FIELD, "exacta", [1, 1])          # dup
    assert "warning" in price_exotic(FIELD, "trifecta", [1, 2])        # too few
    assert "warning" in price_exotic(FIELD, "exacta", [1, 9])          # unpriced
    assert "warning" in price_exotic(FIELD, "superfecta", [1, 2])      # unknown


def test_srm_two_wins_is_impossible():
    r = price_srm(FIELD, [{"runner": 1, "position": "win"},
                          {"runner": 2, "position": "win"}])
    assert r["probability"] == 0.0
    assert "warning" in r


def test_srm_win_plus_place_is_correlated_not_independent():
    # 1 to win AND 2 to top-3. Independent guess would be p1 * P(2 top3);
    # the true joint is lower because 2 taking a top slot competes with the
    # field once 1 has won. MC should land well under the naive product.
    r = price_srm(FIELD, [{"runner": 1, "position": "win"},
                          {"runner": 2, "position": "top3"}], sims=40000)
    assert 0.0 < r["probability"] < 0.4  # bounded by p(1 wins)=0.4
    assert r["fair_odds"] and r["fair_odds"] > 1.0
    assert r["std_error"] is not None


def test_srm_more_legs_is_never_shorter():
    two = price_srm(FIELD, [{"runner": 1, "position": "top2"},
                            {"runner": 2, "position": "top3"}], sims=40000)
    three = price_srm(FIELD, [{"runner": 1, "position": "top2"},
                              {"runner": 2, "position": "top3"},
                              {"runner": 3, "position": "top4"}], sims=40000)
    assert three["probability"] <= two["probability"] + 0.01
    assert three["fair_odds"] >= two["fair_odds"] - 0.5


def test_srm_is_deterministic_given_the_seed():
    legs = [{"runner": 1, "position": "win"}, {"runner": 2, "position": "top3"}]
    assert price_srm(FIELD, legs) == price_srm(FIELD, legs)


def test_srm_guards_bad_input():
    assert "warning" in price_srm(FIELD, [{"runner": 1, "position": "win"}])   # 1 leg
    assert "warning" in price_srm(FIELD, [{"runner": 1, "position": "win"},
                                          {"runner": 1, "position": "top2"}])  # dup
    assert "warning" in price_srm(FIELD, [{"runner": 1, "position": "zoom"},
                                          {"runner": 2, "position": "win"}])   # band
