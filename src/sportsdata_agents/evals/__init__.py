"""Eval harness (M2.4, §16.3): scored quality gates over golden datasets."""

from __future__ import annotations

from sportsdata_agents.evals.runner import EvalScore, gate_against_baseline, load_baseline, run_offline_evals

__all__ = ["EvalScore", "gate_against_baseline", "load_baseline", "run_offline_evals"]
