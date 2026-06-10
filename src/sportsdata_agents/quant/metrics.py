"""Deterministic quant metrics (M2.2/M2.4, P8 — math, never an LLM opinion).

Shared by the ``calibration_metrics`` native tool, the backtester, and the eval
harness, so "calibration" means exactly one thing everywhere.
"""

from __future__ import annotations

import math
from typing import Any

EPS = 1e-12  # log-loss clamp; a confident-and-wrong 0/1 prob must not become -inf


def brier_score(pairs: list[tuple[float, int]]) -> float:
    """Mean squared error of probabilities vs outcomes (0 = perfect, 0.25 = coin-flip
    prior on a balanced set). ``pairs`` = [(prob, outcome 0|1), ...]."""
    if not pairs:
        raise ValueError("brier_score needs at least one (prob, outcome) pair")
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs)


def log_loss(pairs: list[tuple[float, int]]) -> float:
    """Negative mean log-likelihood; punishes confident wrongness hardest."""
    if not pairs:
        raise ValueError("log_loss needs at least one (prob, outcome) pair")
    total = 0.0
    for p, o in pairs:
        p = min(max(p, EPS), 1 - EPS)
        total += math.log(p) if o == 1 else math.log(1 - p)
    return -total / len(pairs)


def validate_pairs(raw: list[dict[str, Any]]) -> list[tuple[float, int]]:
    """Tool-facing validation: [{prob, outcome}] → [(float, 0|1)], loud on junk."""
    pairs: list[tuple[float, int]] = []
    for i, row in enumerate(raw):
        try:
            prob = float(row["prob"])
            outcome = int(row["outcome"])
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"pair {i}: needs numeric `prob` and 0/1 `outcome` ({e})") from e
        if not 0.0 <= prob <= 1.0:
            raise ValueError(f"pair {i}: prob {prob} outside [0, 1]")
        if outcome not in (0, 1):
            raise ValueError(f"pair {i}: outcome must be 0 or 1")
        pairs.append((prob, outcome))
    return pairs


def calibration_report(raw: list[dict[str, Any]]) -> dict[str, Any]:
    """{brier, log_loss, n} for [{prob, outcome}] — the M2.2 calibration record."""
    pairs = validate_pairs(raw)
    return {
        "brier": round(brier_score(pairs), 6),
        "log_loss": round(log_loss(pairs), 6),
        "n": len(pairs),
    }
