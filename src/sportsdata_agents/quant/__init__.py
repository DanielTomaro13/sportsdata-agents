"""Deterministic quant layer (P8): metrics, value math, backtesting — no LLM inside."""

from __future__ import annotations

from sportsdata_agents.quant.metrics import brier_score, calibration_report, log_loss

__all__ = ["brier_score", "calibration_report", "log_loss"]
