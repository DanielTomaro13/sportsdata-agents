"""
Heuristic firm-score (v1, no ML) — a transparent, rule-based prediction of which
runners will SHORTEN before the jump.

It ships before the model so there's something predictive on screen immediately, and
it is the baseline the LightGBM model must beat out-of-sample (B-heuristic in the
plan). Every input is knowable ≥1hr out; the score is deliberately simple and
explainable — a weighted blend of the firming precursors the tool already trusts:

  market respect  (favouritism — money already rates it)   w=0.35
  recent form     (TAB form rating, field-relative)         w=0.25
  money flow      (Betfair WoM + pool-share momentum)       w=0.25
  consensus       (tipped / expert best-bet)                w=0.15
  × momentum adj  (graded by pool-share delta, not binary)

v1.1 adds the money-flow factor: Betfair weight-of-money (backers stacking a
runner's queue precede a shorten — the same signal the confirmation ticks
trust) blended with the tote pool-share delta since open. The momentum
multiplier is now graded by the size of the share move instead of a flat
firming/drifting step. Everything stays knowable pre-jump, pure, and fully
explained in the factors breakdown — the DataLogger records the same fields,
so the LightGBM model trains on exactly what the heuristic sees.

Connections (jockey/trainer strike-rate) remain the next add — no data join yet.
"""

from __future__ import annotations

from typing import Any

W_MARKET = 0.35
W_FORM = 0.25
W_FLOW = 0.25
W_CONSENSUS = 0.15

# Momentum adjustment: a runner already firming is more likely to keep firming; one
# already drifting against the market is less likely to turn. Graded: the flat
# step is the floor/ceiling, the pool-share delta scales within it.
ADJ_FIRMING = 1.10
ADJ_DRIFTING = 0.75
# A share move of this much (absolute) saturates the graded adjustment.
_SHARE_SAT = 0.04

TIERS = ((0.66, "STRONG"), (0.48, "WARM"), (0.32, "LEAN"))


def _price(r: dict[str, Any]) -> float | None:
    return r.get("corp_best") or r.get("fixed_win") or r.get("tote_win")


def _tier(score: float) -> str:
    for cut, label in TIERS:
        if score >= cut:
            return label
    return "—"


def firm_scores(runners: list[dict[str, Any]], tip_numbers: set[int] | None = None
                ) -> dict[int, dict[str, Any]]:
    """Score every (active) runner's firm-likelihood, field-relative. Returns
    {number: {score, tier, factors}}. Pure — safe to call live and for logging."""
    tip_numbers = tip_numbers or set()
    active = [r for r in runners if not r.get("scratched")]
    if not active:
        return {}

    # Market respect: implied prob from best price, normalised so the favourite = 1.
    implied = {r["number"]: (1.0 / _price(r)) for r in active if _price(r)}
    max_imp = max(implied.values(), default=0.0)

    # Recent form: field-relative min-max of the TAB form rating (missing → neutral).
    ratings = {r["number"]: r["form_rating"] for r in active
               if isinstance(r.get("form_rating"), (int, float))}
    lo = min(ratings.values(), default=0.0)
    hi = max(ratings.values(), default=0.0)
    span = hi - lo

    out: dict[int, dict[str, Any]] = {}
    for r in active:
        num = r["number"]
        m = (implied.get(num, 0.0) / max_imp) if max_imp > 0 else 0.0
        if num in ratings and span > 0:
            f = (ratings[num] - lo) / span
        else:
            f = 0.5
        c = min(1.0, (0.6 if num in tip_numbers else 0.0) + (0.4 if r.get("best_bet") else 0.0))

        # Money flow: WoM is the fraction of queued exchange money on the back
        # side (0.5 = balanced; backers stacking above it precede a shorten),
        # blended with the pool-share delta since open. Missing data = neutral.
        wom = r.get("bf_wom")
        wom_sig = max(0.0, min(1.0, (wom - 0.5) * 2 + 0.5)) if wom is not None else 0.5
        sd = r.get("share_delta")
        sd_sig = max(0.0, min(1.0, sd / _SHARE_SAT * 0.5 + 0.5)) if sd is not None else 0.5
        flow = 0.6 * wom_sig + 0.4 * sd_sig

        base = W_MARKET * m + W_FORM * f + W_FLOW * flow + W_CONSENSUS * c
        direction = r.get("direction")
        if direction in ("firming", "drifting") and sd is not None:
            # graded: a whisper of movement nudges, a surge saturates
            g = max(0.0, min(1.0, abs(sd) / _SHARE_SAT))
            adj = 1.0 + (ADJ_FIRMING - 1.0) * g if direction == "firming"                 else 1.0 - (1.0 - ADJ_DRIFTING) * g
        else:
            adj = ADJ_FIRMING if direction == "firming" else ADJ_DRIFTING if direction == "drifting" else 1.0
        score = max(0.0, min(1.0, base * adj))

        out[num] = {
            "score": round(score, 3),
            "tier": _tier(score),
            "factors": {"market": round(m, 2), "form": round(f, 2),
                        "flow": round(flow, 2), "consensus": round(c, 2),
                        "momentum": round(adj, 3)},
        }
    return out
