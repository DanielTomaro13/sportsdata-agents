"""Same-game-multi pricing: engine when connected (correlated), else an honest
independent-product fallback."""

from __future__ import annotations

from sportsdata_agents.quant.sgm import price_sgm, price_sgm_independent


def test_independent_is_the_product_of_leg_probs():
    r = price_sgm_independent([{"label": "Home win", "prob": 0.6},
                               {"label": "Over 210.5", "prob": 0.5}])
    assert abs(r["fair_probability"] - 0.30) < 1e-9
    assert r["fair_odds"] == round(1 / 0.30, 2)
    assert r["correlation_lift"] == 1.0
    assert r["priced_by"] == "independent"
    assert r["warnings"]  # says it's not correlated


def test_independent_guards_bad_legs():
    assert "warning" in price_sgm_independent([{"label": "solo", "prob": 0.5}])
    assert "warning" in price_sgm_independent([{"label": "a", "prob": 1.5},
                                               {"label": "b", "prob": 0.5}])
    assert "warning" in price_sgm_independent([{"label": "a"},
                                               {"label": "b", "prob": 0.5}])


def test_price_sgm_uses_a_connected_engine():
    class FakeEngine:
        def sgm_quote(self, sport, fixture_id, quotes, legs):
            # a real engine returns a CORRELATED joint, shorter than independent
            return {"fair_probability": 0.36, "fair_odds": 2.78,
                    "independent_probability": 0.30, "correlation_lift": 1.2,
                    "warnings": []}
    r = price_sgm("nba", "fx1", {}, [{"prob": 0.6}, {"prob": 0.5}],
                  engine=FakeEngine())
    assert r["priced_by"] == "engine"
    assert r["correlation_lift"] == 1.2
    assert r["fair_probability"] == 0.36  # engine's correlated price, not 0.30


def test_price_sgm_falls_back_when_no_engine():
    r = price_sgm("nba", "fx1", {}, [{"label": "A", "prob": 0.6},
                                     {"label": "B", "prob": 0.5}], engine=None)
    # resolve_engine() is 'none' by default in tests -> independent fallback
    assert r["priced_by"] == "independent"
    assert abs(r["fair_probability"] - 0.30) < 1e-9


def test_price_sgm_survives_a_broken_engine():
    class BrokenEngine:
        def sgm_quote(self, *a, **k):
            raise RuntimeError("model blew up")
    r = price_sgm("nba", "fx1", {}, [{"prob": 0.6}, {"prob": 0.5}],
                  engine=BrokenEngine())
    assert r["priced_by"] == "independent"
    assert r["engine_error"] == "model blew up"
