"""Kalshi vs Polymarket on the SAME question — the model-free value signal for
events no bookmaker offers (elections, geopolitics, crypto levels, science).

Neither platform is a resolver-mapped fixture (their niche questions have no
bookmaker equivalent and no engine model), so they are paired DIRECTLY here:
the English event title token-matches across platforms, then each positive
outcome (a candidate, or the YES side) is matched by its own tokens. When the
two real-money order books disagree on a matched outcome beyond a threshold,
that is an actionable value flag — back the outcome on whichever platform
offers the higher odds, using the other's price as fair.

HONESTY, because cross-platform resolution risk is real (each gate below was
earned by a live false positive, not imagined):
- Questions must match STRONGLY (default 0.7): at 0.6 "2028 Republican
  presidential nominee" paired with "Republican VP Nominee 2028" and "OpenAI's
  IPO" with "Anthropic's IPO".
- Year tokens must agree EXACTLY, absent-on-both included: Kalshi's "Colorado
  Senate winner? (2028)" paired with Polymarket's un-yeared Colorado Senate
  market — which is the 2026 race — and read a +681% phantom edge.
- "A vs B" game questions are excluded: both platforms price the same games,
  but one side's team-named outcome can come from a spread/alt market while
  the other's is the moneyline — a category mismatch masquerading as edge.
  Games flow through the fixture resolver + exchange scans instead.
- Extreme longshots are excluded (a de-vig on a 1c contract is noise).
- A platform reporting no volume on a market is skipped (a dead quote invents
  disagreements).
- The two platforms may still word or settle a question differently — every
  candidate says so; it is a lead to verify, not a guaranteed arb.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sportsdata_agents.data.models import OddsSnapshot

__all__ = ["scan_prediction_disagreements"]

_TOKEN_RE = re.compile(r"[a-z0-9]+")
# fillers only — NUMBERS and years are kept, they distinguish "…2028" from
# "…2026" senate races and "1AM" from "2AM" crypto windows
_STOP = frozenset({
    "the", "a", "an", "of", "in", "to", "by", "will", "be", "is", "are", "on",
    "at", "for", "and", "or", "vs", "who", "which", "what", "out", "as",
})


_YEAR_RE = re.compile(r"^(19|20)\d\d$")
_VS_RE = re.compile(r"\bvs\.?\b|\bv\.?\b(?=\s[A-Z])")


def _tokens(text: str) -> frozenset[str]:
    return frozenset(t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOP and len(t) > 1)


def _years(tokens: frozenset[str]) -> frozenset[str]:
    return frozenset(t for t in tokens if _YEAR_RE.match(t))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _volume(meta: dict[str, Any]) -> float:
    for key in ("volume_24h", "liquidity", "open_interest"):
        v = meta.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


@dataclass
class _Unit:
    """One platform's take on one question: positive outcomes only."""

    provider: str
    event_id: str
    question: str
    q_tokens: frozenset[str]
    last_seen: dt.datetime
    volume: float = 0.0
    # positive-side outcome label -> (odds, tokens)
    outcomes: dict[str, tuple[float, frozenset[str]]] = field(default_factory=dict)


def _as_utc(when: dt.datetime) -> dt.datetime:
    return when if when.tzinfo else when.replace(tzinfo=dt.UTC)


async def scan_prediction_disagreements(
    session: AsyncSession,
    *,
    hours: float = 6.0,
    min_edge_pct: float = 8.0,
    q_threshold: float = 0.7,
    o_threshold: float = 0.5,
    prob_band: tuple[float, float] = (0.03, 0.97),
    min_volume: float = 1.0,
    max_staleness_minutes: float = 30.0,
    limit: int = 20,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or dt.datetime.now(dt.UTC)
    cutoff = now - dt.timedelta(hours=hours)
    rows = (await session.execute(
        select(OddsSnapshot).where(
            OddsSnapshot.provider.in_(("kalshi", "polymarket")),
            OddsSnapshot.captured_at > cutoff,
        ).order_by(OddsSnapshot.captured_at)
    )).scalars().all()

    units: dict[tuple[str, str], _Unit] = {}
    for row in rows:
        selection = str(row.selection)
        if selection.startswith("no "):
            continue  # the negative side is redundant with its positive twin
        question = str(row.event_name or "").strip()
        if not question or _VS_RE.search(question):
            continue  # "A vs B" games go through the fixture path, not here
        key = (row.provider, row.event_external_id)
        unit = units.get(key)
        if unit is None:
            unit = units[key] = _Unit(
                provider=row.provider, event_id=row.event_external_id,
                question=question, q_tokens=_tokens(question),
                last_seen=_as_utc(row.captured_at))
        unit.last_seen = max(unit.last_seen, _as_utc(row.captured_at))
        unit.volume = max(unit.volume, _volume(row.meta or {}))
        # ascending capture order → last write per label wins (freshest odds)
        unit.outcomes[selection] = (float(row.odds), _tokens(selection))

    kalshi = [u for u in units.values() if u.provider == "kalshi" and u.q_tokens and u.outcomes]
    poly = [u for u in units.values() if u.provider == "polymarket" and u.q_tokens and u.outcomes]
    lo, hi = prob_band
    stale_bound = dt.timedelta(minutes=max_staleness_minutes)

    found: list[dict[str, Any]] = []
    for ku in kalshi:
        if ku.volume < min_volume or now - ku.last_seen > stale_bound:
            continue
        # the single best Polymarket question by token overlap; a tie or a weak
        # top match is not a confident pairing, so it is skipped
        ranked = sorted(
            ((_jaccard(ku.q_tokens, pu.q_tokens), pu) for pu in poly),
            key=lambda t: -t[0])
        if not ranked or ranked[0][0] < q_threshold:
            continue
        if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.15:
            continue  # ambiguous — two Polymarket questions match equally well
        pu = ranked[0][1]
        if _years(ku.q_tokens) != _years(pu.q_tokens):
            continue  # "(2028)" vs an un-yeared twin = likely a DIFFERENT cycle
        if pu.volume < min_volume or now - pu.last_seen > stale_bound:
            continue
        for k_label, (k_odds, k_tokens) in ku.outcomes.items():
            # match this Kalshi outcome to its best Polymarket outcome
            best_score, best = 0.0, None
            for p_label, (p_odds, p_tokens) in pu.outcomes.items():
                score = 1.0 if k_label == p_label else _jaccard(k_tokens, p_tokens)
                if score > best_score:
                    best_score, best = score, (p_odds, p_tokens)
            if best is None or best_score < o_threshold:
                continue
            p_odds = best[0]
            pk, pp = 1.0 / k_odds, 1.0 / p_odds
            if not (lo <= pk <= hi and lo <= pp <= hi):
                continue  # a longshot on either side — de-vig is untrustworthy
            # back the outcome on the higher-odds platform, the other = fair
            if k_odds >= p_odds:
                edge_pct = (k_odds * pp - 1.0) * 100.0
                back, back_odds, fair_odds = "Kalshi", k_odds, p_odds
            else:
                edge_pct = (p_odds * pk - 1.0) * 100.0
                back, back_odds, fair_odds = "Polymarket", p_odds, k_odds
            if edge_pct < min_edge_pct:
                continue
            found.append({
                "question": ku.question,
                "poly_question": pu.question,
                "outcome": k_label,
                "back": back,
                "back_odds": round(back_odds, 3),
                "fair_odds": round(fair_odds, 3),
                "kalshi_odds": round(k_odds, 3),
                "polymarket_odds": round(p_odds, 3),
                "edge_pct": round(edge_pct, 2),
                "q_match": round(ranked[0][0], 2),
                "kalshi_event": ku.event_id,
                "polymarket_event": pu.event_id,
            })
    # two Kalshi events can pair with the same Polymarket question (series
    # variants); keep only the strongest signal per (poly question, outcome)
    found.sort(key=lambda c: -c["edge_pct"])
    seen_pairs: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for c in found:
        pair = (c["polymarket_event"], c["outcome"])
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        deduped.append(c)
    return deduped[:limit]
