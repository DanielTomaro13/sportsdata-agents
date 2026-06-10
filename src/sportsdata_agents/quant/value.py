"""Value-finder math (M2.3, P8): model prob vs vig-removed market — pure functions.

EV per unit staked at decimal odds o with true probability p is ``p*o - 1``; the
market's own opinion is its implied probability normalised for the overround. A
"value" selection is one where the model's edge clears the threshold — this module
only computes; whether anyone acts on it is (always) the user's decision.
"""

from __future__ import annotations

from typing import Any


def find_value(
    market: list[dict[str, Any]],
    model_probs: list[dict[str, Any]],
    *,
    min_edge_pct: float = 2.0,
) -> dict[str, Any]:
    """{selections: [...], value: [names]} for one market.

    ``market`` = every selection's price ([{name, odds}] — the FULL market, or the
    vig removal is wrong); ``model_probs`` = [{name, prob}] from a calibrated model.
    """
    if not market:
        raise ValueError("market must list every selection's odds")
    odds_by_name: dict[str, float] = {}
    for entry in market:
        market_odds = float(entry["odds"])
        if market_odds < 1.01:
            raise ValueError(f"odds {market_odds} for {entry.get('name')!r} below 1.01")
        odds_by_name[str(entry["name"])] = market_odds
    probs_by_name = {str(r["name"]): float(r["prob"]) for r in model_probs}
    for name, model_prob in probs_by_name.items():
        if not 0.0 <= model_prob <= 1.0:
            raise ValueError(f"prob {model_prob} for {name!r} outside [0, 1]")
        if name not in odds_by_name:
            raise ValueError(f"model prob for {name!r} has no market price")

    overround = sum(1.0 / o for o in odds_by_name.values())
    selections: list[dict[str, Any]] = []
    value_names: list[str] = []
    for name, odds in odds_by_name.items():
        implied = 1.0 / odds
        fair = implied / overround
        row: dict[str, Any] = {
            "name": name,
            "odds": odds,
            "implied_prob": round(implied, 4),
            "fair_prob": round(fair, 4),  # the market's vig-free opinion
        }
        maybe_prob = probs_by_name.get(name)
        if maybe_prob is not None:
            edge_pct = (maybe_prob * odds - 1.0) * 100.0
            row |= {
                "model_prob": maybe_prob,
                "edge_pct": round(edge_pct, 2),
                "fair_odds": round(1.0 / maybe_prob, 3) if maybe_prob > 0 else None,
                "value": edge_pct >= min_edge_pct,
            }
            if row["value"]:
                value_names.append(name)
        selections.append(row)
    return {
        "overround_pct": round((overround - 1.0) * 100.0, 2),
        "min_edge_pct": min_edge_pct,
        "selections": selections,
        "value": value_names,
    }
