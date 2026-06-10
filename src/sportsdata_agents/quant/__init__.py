"""Deterministic quant layer (P8): metrics, value math, backtesting — no LLM inside."""

from __future__ import annotations

from sportsdata_agents.quant.backtest import run_backtest
from sportsdata_agents.quant.metrics import brier_score, calibration_report, log_loss
from sportsdata_agents.quant.value import find_value

__all__ = ["brier_score", "calibration_report", "find_value", "log_loss", "run_backtest"]
