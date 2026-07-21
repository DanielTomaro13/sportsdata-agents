"""Same-game-multi pricing for the sports board's "generate price" button.

A same-game multi combines legs WITHIN one game (team to win + total over +
a player prop). Unlike a cross-game multi, the legs are correlated — a
blowout win drags the total up, a star's big night lifts both his points and
the win — so the true joint price is NOT the product of the leg prices.

The correlation model lives in the sportsdata racing/sports ENGINE
(``PricingEngine.sgm_quote``), so when an engine is connected we ask it: it
returns the joint fair with the correlation lift made explicit (how much the
correlation moved the price off the independent product). When no engine is
configured, we fall back to the INDEPENDENT product of the legs' own fair
probabilities and say so — an honest floor/ceiling, not a correlated price.
"""

from __future__ import annotations

from typing import Any

__all__ = ["price_sgm", "price_sgm_independent"]


def price_sgm_independent(legs: list[dict[str, Any]]) -> dict[str, Any]:
    """Joint price assuming the legs are INDEPENDENT — the product of each
    leg's fair probability. ``legs`` = [{"label": str, "prob": p}, …]. This is
    the no-engine fallback: real correlation usually makes the true price
    shorter (positively correlated legs) so this reads as a conservative
    ceiling on the odds."""
    if len(legs) < 2:
        return {"warning": "a same-game multi needs at least 2 legs"}
    joint = 1.0
    used = []
    for leg in legs:
        try:
            p = float(leg.get("prob"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return {"warning": f"leg {leg.get('label', '?')!r} has no fair probability"}
        if not 0.0 < p <= 1.0:
            return {"warning": f"leg {leg.get('label', '?')!r} probability out of range"}
        joint *= p
        used.append({"label": leg.get("label", ""), "prob": round(p, 4)})
    if joint <= 0:
        return {"warning": "joint probability is zero"}
    return {
        "fair_probability": joint,
        "fair_odds": round(1.0 / joint, 2),
        "independent_probability": joint,
        "correlation_lift": 1.0,        # none — this IS the independent price
        "legs": used,
        "priced_by": "independent",
        "warnings": ["no engine connected — legs priced independently; a real "
                     "same-game multi is usually SHORTER than this"],
    }


def price_sgm(
    sport: str, fixture_id: str, quotes: dict[str, Any],
    legs: list[dict[str, Any]], *, engine: Any = None,
) -> dict[str, Any]:
    """Price a same-game multi. Uses the connected engine's correlated
    ``sgm_quote`` when available (the real price); otherwise the independent
    product of the legs' fair probabilities, clearly flagged.

    ``legs`` for the engine follow its own leg shape (market/selection/line);
    each leg SHOULD also carry a ``prob`` so the independent fallback can price
    it if no engine is configured."""
    from sportsdata_agents.quant.engines import EngineUnavailable, resolve_engine

    eng = engine if engine is not None else resolve_engine()
    if eng is not None:
        try:
            out = dict(eng.sgm_quote(sport, fixture_id, quotes, legs))
            out.setdefault("priced_by", "engine")
            return out
        except EngineUnavailable:
            pass
        except Exception as exc:  # a bad engine call must not sink the button
            return {**price_sgm_independent(legs),
                    "engine_error": str(exc)}
    return price_sgm_independent(legs)
