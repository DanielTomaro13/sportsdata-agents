"""M2.2 — calibration metrics (exact values) + the native tool + spec/skills wiring."""

from __future__ import annotations

import math

import pytest

from sportsdata_agents.quant.metrics import brier_score, calibration_report, log_loss

pytestmark = pytest.mark.unit


def test_brier_exact_values() -> None:
    assert brier_score([(1.0, 1), (0.0, 0)]) == 0.0  # oracle
    assert brier_score([(0.5, 1), (0.5, 0)]) == pytest.approx(0.25)  # coin flip
    # hand-computed: (0.8-1)^2 + (0.3-0)^2 + (0.6-1)^2 = 0.04+0.09+0.16 → /3
    assert brier_score([(0.8, 1), (0.3, 0), (0.6, 1)]) == pytest.approx(0.29 / 3)


def test_log_loss_exact_and_clamped() -> None:
    assert log_loss([(0.5, 1), (0.5, 0)]) == pytest.approx(math.log(2))
    # hand-computed: -(ln 0.8 + ln 0.7) / 2
    assert log_loss([(0.8, 1), (0.3, 0)]) == pytest.approx(-(math.log(0.8) + math.log(0.7)) / 2)
    # a confident-and-wrong 0.0 must clamp, not blow up to inf
    assert log_loss([(0.0, 1)]) < 30


def test_empty_pairs_loud() -> None:
    with pytest.raises(ValueError, match="at least one"):
        brier_score([])
    with pytest.raises(ValueError, match="at least one"):
        log_loss([])


def test_calibration_report_validates_junk() -> None:
    with pytest.raises(ValueError, match="outside"):
        calibration_report([{"prob": 1.4, "outcome": 1}])
    with pytest.raises(ValueError, match="0 or 1"):
        calibration_report([{"prob": 0.5, "outcome": 2}])
    with pytest.raises(ValueError, match="pair 0"):
        calibration_report([{"prob": "nope", "outcome": 1}])
    report = calibration_report([{"prob": 0.5, "outcome": 1}, {"prob": 0.5, "outcome": 0}])
    assert report == {"brier": 0.25, "log_loss": pytest.approx(round(math.log(2), 6)), "n": 2}


async def test_calibration_metrics_native_tool() -> None:
    from sportsdata_agents.tools.registry import NATIVE_TOOLS

    out = await NATIVE_TOOLS["calibration_metrics"].execute(
        {"pairs": [{"prob": 0.9, "outcome": 1}, {"prob": 0.2, "outcome": 0}]}
    )
    assert out["n"] == 2
    assert out["brier"] == pytest.approx((0.01 + 0.04) / 2)


def test_modelling_spec_and_skills_wire_up() -> None:
    from sportsdata_agents.agents.loader import lint_specs, load_builtin_specs
    from sportsdata_agents.agents.skills import load_skillset

    specs = load_builtin_specs()
    spec = specs["modelling"]
    assert spec.sandbox == "ephemeral"  # run_python gating
    assert {"run_python", "calibration_metrics", "save_model", "record_predictions"} <= set(spec.tools.native)
    assert "modelling" in specs["orchestrator"].can_delegate_to
    assert lint_specs(specs) == []

    skills = load_skillset(["build_a_totals_model", "calibrate_probabilities"], None)
    assert {s.name for s in skills.newly_triggered("please build a totals model")} == {"build_a_totals_model"}
    assert {s.name for s in skills.newly_triggered("is this calibrated? check the brier")} == {
        "calibrate_probabilities"
    }
