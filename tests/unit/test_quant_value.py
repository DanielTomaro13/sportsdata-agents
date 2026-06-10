"""M2.3 — value-finder math (exact, deterministic) + the native tool wiring."""

from __future__ import annotations

import pytest

from sportsdata_agents.quant.value import find_value

pytestmark = pytest.mark.unit

MARKET = [{"name": "home", "odds": 1.85}, {"name": "away", "odds": 2.05}]


def test_value_finder_exact_math() -> None:
    out = find_value(MARKET, [{"name": "home", "prob": 0.58}, {"name": "away", "prob": 0.42}],
                     min_edge_pct=2.0)
    # overround: 1/1.85 + 1/2.05 = 1.028346 → 2.83%
    assert out["overround_pct"] == pytest.approx(2.83, abs=0.01)
    home = next(s for s in out["selections"] if s["name"] == "home")
    assert home["implied_prob"] == pytest.approx(0.5405, abs=1e-4)
    assert home["fair_prob"] == pytest.approx(0.5256, abs=1e-4)  # vig removed
    assert home["edge_pct"] == pytest.approx(7.3, abs=0.01)  # 0.58 * 1.85 - 1
    assert home["fair_odds"] == pytest.approx(1.724, abs=1e-3)
    assert home["value"] is True
    away = next(s for s in out["selections"] if s["name"] == "away")
    assert away["edge_pct"] == pytest.approx(-13.9, abs=0.01)
    assert away["value"] is False
    assert out["value"] == ["home"]


def test_value_finder_threshold_and_partial_probs() -> None:
    # an edge below the threshold is reported but not flagged
    out = find_value(MARKET, [{"name": "home", "prob": 0.55}], min_edge_pct=5.0)
    home = next(s for s in out["selections"] if s["name"] == "home")
    assert home["edge_pct"] == pytest.approx(1.75, abs=0.01)
    assert out["value"] == []
    # selections without a model prob still get market math, no value verdict
    away = next(s for s in out["selections"] if s["name"] == "away")
    assert "model_prob" not in away and "value" not in away


def test_value_finder_validates_inputs() -> None:
    with pytest.raises(ValueError, match="every selection"):
        find_value([], [{"name": "home", "prob": 0.5}])
    with pytest.raises(ValueError, match=r"below 1\.01"):
        find_value([{"name": "home", "odds": 0.9}], [])
    with pytest.raises(ValueError, match="outside"):
        find_value(MARKET, [{"name": "home", "prob": 1.2}])
    with pytest.raises(ValueError, match="no market price"):
        find_value(MARKET, [{"name": "draw", "prob": 0.1}])


async def test_value_finder_native_tool() -> None:
    from sportsdata_agents.tools.registry import NATIVE_TOOLS

    out = await NATIVE_TOOLS["value_finder"].execute(
        {"market": MARKET, "model_probs": [{"name": "home", "prob": 0.58}]}
    )
    assert out["value"] == ["home"]


def test_quant_specs_lint_and_delegates() -> None:
    from sportsdata_agents.agents.loader import lint_specs, load_builtin_specs

    specs = load_builtin_specs()
    assert {"value_scout", "backtester"} <= set(specs)
    assert {"value_scout", "backtester"} <= set(specs["orchestrator"].can_delegate_to)
    assert "value_finder" in specs["value_scout"].tools.native
    assert "run_backtest" in specs["backtester"].tools.native
    assert lint_specs(specs) == []
