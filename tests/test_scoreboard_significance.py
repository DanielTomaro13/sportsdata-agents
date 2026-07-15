"""The significance layer must separate edge from luck — and say so honestly.

_significance() answers two questions per section from the individual settled
bets: how much does the ROI wobble (bootstrap CI), and how often would a FAIR
market (win prob = 1/odds) produce a run this good (Monte Carlo p)."""

from __future__ import annotations

import random

from sportsdata_agents.quant.scoreboard import _SIG_MIN_SETTLED, _significance


def test_small_samples_refuse_to_judge():
    bets = [(1.0, 1.0, 2.0)] * (_SIG_MIN_SETTLED - 1)
    assert _significance(bets) is None


def test_zero_stake_and_degenerate_odds_rows_are_ignored():
    # only 3 real rows survive the filter -> below the floor -> None
    bets = [(0.0, 0.0, 2.0)] * 10 + [(1.0, 1.0, 1.0)] * 10 + [(1.0, 1.0, 2.0)] * 3
    assert _significance(bets) is None


def test_a_clear_edge_reads_as_edge():
    # 60% hit rate at evens over 200 bets — a real, large edge
    rng = random.Random(7)
    bets = [(1.0, 1.0 if rng.random() < 0.60 else -1.0, 2.0) for _ in range(200)]
    sig = _significance(bets)
    assert sig is not None
    assert sig["n"] == 200
    assert sig["p_fair_market"] < 0.05
    lo, hi = sig["roi_ci95"]
    assert lo > 0 and hi > lo
    assert sig["verdict"] == "edge"


def test_a_fair_market_run_reads_as_luck():
    # win prob exactly 1/odds — the null itself; ROI hovers near zero
    rng = random.Random(11)
    bets = [(1.0, 2.5 if rng.random() < 1 / 3.5 else -1.0, 3.5) for _ in range(300)]
    sig = _significance(bets)
    assert sig is not None
    assert sig["p_fair_market"] > 0.05
    lo, hi = sig["roi_ci95"]
    assert lo < 0 < hi  # the CI straddles zero: no verdict of edge
    assert sig["verdict"] != "edge"


def test_deterministic_given_the_same_bets():
    bets = [(1.0, 1.0, 2.0), (1.0, -1.0, 2.0)] * 10
    assert _significance(bets) == _significance(bets)


def test_losing_run_never_reads_as_edge():
    bets = [(1.0, -1.0, 5.0)] * 50
    sig = _significance(bets)
    assert sig is not None
    assert sig["verdict"] == "indistinguishable from luck" or sig["p_fair_market"] >= 0.05
    assert sig["roi_ci95"][1] <= 0  # even the CI's top is a loss
