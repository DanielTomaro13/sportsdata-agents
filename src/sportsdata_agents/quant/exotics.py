"""Exotic and same-race-multi pricing from win probabilities.

The board already has a win-probability opinion per runner (the sportsdata
racing engine's form model, else de-vigged Betfair/tote). From those, the
whole exotic tree and same-race multis follow — no extra data, just the
ordering model:

- **Exotics** (exacta / quinella / trifecta / first-4) are priced CLOSED-FORM
  with the Harville model: given win probs p_i (Σ = 1), the chance the field
  finishes i, j, k, … in that order is
      p_i · p_j/(1-p_i) · p_k/(1-p_i-p_j) · …
  i.e. each place is a fresh win contest among the runners not yet placed.
  Box bets sum over every ordering of the chosen set.

- **Same-race multis** (pick runners to each WIN or finish top-N) are priced
  by **Plackett-Luce Monte Carlo**: simulate finishing orders (repeatedly draw
  the next finisher proportional to the remaining runners' weights) and count
  how often every leg lands. Simulation is used here, not closed form, because
  the legs are correlated in ways that don't factorise — two runners can't both
  win, and one placing shifts the others' chances — and MC captures that
  exactly. The price carries a Monte-Carlo std_error; treat a difference inside
  the band as noise, never edge.

Fair price = 1 / probability. An optional ``margin`` (bookmaker overround, as a
fraction) shortens the returned price to what a book WOULD offer:
price = (1 - margin) / probability — so the board can show both the fair line
and a realistic offer.
"""

from __future__ import annotations

import itertools
import math
import random
from typing import Any

__all__ = [
    "EXOTIC_LEGS",
    "normalize_win_probs",
    "price_exotic",
    "price_srm",
]

# how many selections each exotic consumes (box bets need >= this many)
EXOTIC_LEGS = {"exacta": 2, "quinella": 2, "trifecta": 3, "first4": 4}
_ORDERED = {"exacta": 2, "trifecta": 3, "first4": 4}  # order matters
_SRM_BANDS = {"win": 1, "top2": 2, "top3": 3, "top4": 4}
_SRM_SIMS = 20000
_SRM_SEED = 20260721


def normalize_win_probs(win_probs: dict[int, float]) -> dict[int, float]:
    """Drop non-positive entries and renormalise to sum 1."""
    clean = {int(k): float(v) for k, v in win_probs.items() if v and v > 0}
    total = sum(clean.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in clean.items()}


def _ordered_prob(probs: dict[int, float], seq: tuple[int, ...]) -> float:
    """Harville probability the field finishes exactly in ``seq`` order."""
    remaining = 1.0
    p = 1.0
    for runner in seq:
        pr = probs.get(runner)
        if not pr or remaining <= 0:
            return 0.0
        p *= pr / remaining
        remaining -= pr
    return p


def _fair(prob: float, margin: float) -> dict[str, Any]:
    if prob <= 0:
        return {"probability": 0.0, "fair_odds": None, "offer_odds": None}
    fair = 1.0 / prob
    offer = (1.0 - margin) / prob if 0.0 <= margin < 1.0 else fair
    return {"probability": prob,  # full precision; only the odds are display-rounded
            "fair_odds": round(fair, 2),
            "offer_odds": round(offer, 2)}


def price_exotic(
    win_probs: dict[int, float], bet_type: str, selection: list[int],
    *, box: bool = False, margin: float = 0.0,
) -> dict[str, Any]:
    """Price one exotic. ``selection`` is the saddle numbers in finishing order
    (ignored order when ``box``). Returns probability + fair/offer odds, or a
    ``warning`` when the bet is malformed."""
    bet_type = bet_type.lower()
    if bet_type not in EXOTIC_LEGS:
        return {"warning": f"unknown exotic {bet_type!r}"}
    probs = normalize_win_probs(win_probs)
    need = EXOTIC_LEGS[bet_type]
    picks = [int(s) for s in selection]
    if len(set(picks)) != len(picks):
        return {"warning": "a runner cannot appear twice in one exotic"}
    if any(p not in probs for p in picks):
        return {"warning": "a selection has no fair price (scratched / unpriced)"}
    if len(picks) < need:
        return {"warning": f"{bet_type} needs at least {need} runners"}

    if bet_type == "quinella":
        # top-2 either way = exacta(a,b) + exacta(b,a); box of >2 sums all pairs
        pairs = itertools.permutations(picks, 2) if box or len(picks) == 2 else \
            itertools.permutations(picks[:2], 2)
        prob = sum(_ordered_prob(probs, pair) for pair in pairs)
        combos = len(picks) * (len(picks) - 1) // 2 if box else 1
        return {**_fair(prob, margin), "bet": "quinella", "runners": picks,
                "combinations": combos}

    depth = _ORDERED[bet_type]
    if box:
        seqs = list(itertools.permutations(picks, depth))
        prob = sum(_ordered_prob(probs, s) for s in seqs)
        return {**_fair(prob, margin), "bet": bet_type, "runners": picks,
                "boxed": True, "combinations": len(seqs)}
    seq = tuple(picks[:depth])
    return {**_fair(_ordered_prob(probs, seq), margin), "bet": bet_type,
            "runners": list(seq), "boxed": False, "combinations": 1}


def _simulate(probs: dict[int, float], depth: int, sims: int,
              rng: random.Random) -> list[tuple[int, ...]]:
    """Plackett-Luce: draw the top ``depth`` finishers, sims times."""
    runners = list(probs)
    weights = [probs[r] for r in runners]
    out: list[tuple[int, ...]] = []
    for _ in range(sims):
        pool = runners[:]
        w = weights[:]
        order: list[int] = []
        for _pos in range(min(depth, len(pool))):
            total = sum(w)
            if total <= 0:
                break
            x = rng.random() * total
            cum = 0.0
            for idx, weight in enumerate(w):
                cum += weight
                if x <= cum:
                    order.append(pool.pop(idx))
                    w.pop(idx)
                    break
        out.append(tuple(order))
    return out


def price_srm(
    win_probs: dict[int, float], legs: list[dict[str, Any]],
    *, sims: int = _SRM_SIMS, margin: float = 0.0, seed: int = _SRM_SEED,
) -> dict[str, Any]:
    """Price a same-race multi. ``legs`` = [{"runner": n, "position":
    win|top2|top3|top4}, …]. Every leg's runner must finish within its band in
    the SAME race — priced by Plackett-Luce Monte Carlo, so correlation (two
    runners can't both win; a place shifts the others) is exact."""
    probs = normalize_win_probs(win_probs)
    if not probs:
        return {"warning": "no priced runners"}
    parsed: list[tuple[int, int]] = []
    seen: set[int] = set()
    for leg in legs:
        try:
            runner = int(leg["runner"])
        except (KeyError, TypeError, ValueError):
            return {"warning": "a leg is missing a runner number"}
        band = _SRM_BANDS.get(str(leg.get("position", "win")).lower())
        if band is None:
            return {"warning": f"unknown leg position {leg.get('position')!r}"}
        if runner not in probs:
            return {"warning": f"runner {runner} has no fair price"}
        if runner in seen:
            return {"warning": "a runner cannot appear in two legs"}
        seen.add(runner)
        parsed.append((runner, band))
    if len(parsed) < 2:
        return {"warning": "a same-race multi needs at least 2 legs"}
    # two legs both demanding WIN is impossible in one race
    if sum(1 for _r, b in parsed if b == 1) > 1:
        return {**_fair(0.0, margin), "bet": "srm", "legs": legs,
                "warning": "two runners cannot both win the same race"}

    depth = max(b for _r, b in parsed)
    rng = random.Random(seed)
    orders = _simulate(probs, depth, sims, rng)
    hits = 0
    for order in orders:
        pos = {runner: i + 1 for i, runner in enumerate(order)}
        if all(pos.get(runner, depth + 1) <= band for runner, band in parsed):
            hits += 1
    prob = hits / sims
    # binomial std error on the probability, propagated to the fair odds
    se_prob = math.sqrt(max(prob * (1.0 - prob), 0.0) / sims)
    std_error = round(se_prob / (prob * prob), 2) if prob > 0 else None
    result = _fair(prob, margin)
    return {**result, "bet": "srm",
            "legs": [{"runner": r, "position": next(k for k, v in _SRM_BANDS.items() if v == b)}
                     for r, b in parsed],
            "std_error": std_error, "sims": sims}
