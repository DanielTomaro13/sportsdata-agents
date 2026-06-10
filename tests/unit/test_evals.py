"""M2.4 exit gate: the eval suite produces scores and FAILS a deliberately-worse change."""

from __future__ import annotations

import pytest

from sportsdata_agents.evals import gate_against_baseline, load_baseline, run_offline_evals
from sportsdata_agents.evals.runner import EvalScore

pytestmark = pytest.mark.unit


async def test_offline_evals_produce_expected_scores() -> None:
    scores = {s.name: s for s in await run_offline_evals()}
    assert scores["calibration"].score == pytest.approx(0.79075)
    assert scores["calibration"].details["n"] == 10
    assert scores["clv_backtest"].score == pytest.approx(0.582)  # avg CLV +8.2% on the golden replay
    assert scores["clv_backtest"].details["bets"] == 2
    assert scores["grounding"].score == 1.0 and scores["grounding"].details["misses"] == []


async def test_gate_passes_against_committed_baseline() -> None:
    assert gate_against_baseline(await run_offline_evals(), load_baseline()) == []


def test_gate_fails_a_deliberately_worse_change() -> None:
    """The point of the harness: a regression cannot pass. A 'model improvement' that
    miscalibrates (worse Brier) trips the calibration gate; quietly DELETING an eval
    trips the missing-eval rule."""
    baseline = load_baseline()
    worse = [
        EvalScore(name="calibration", score=0.70, details={}),  # worse Brier
        EvalScore(name="clv_backtest", score=baseline["clv_backtest"], details={}),
        EvalScore(name="grounding", score=baseline["grounding"], details={}),
    ]
    problems = gate_against_baseline(worse, baseline)
    assert len(problems) == 1 and problems[0].startswith("calibration:")

    dropped = [s for s in worse if s.name != "grounding"]
    problems = gate_against_baseline(dropped, baseline)
    assert any("grounding: eval missing" in p for p in problems)


def test_gate_tolerance_absorbs_noise() -> None:
    baseline = {"calibration": 0.79075}
    wiggle = [EvalScore(name="calibration", score=0.79074, details={})]
    assert gate_against_baseline(wiggle, baseline) == []
