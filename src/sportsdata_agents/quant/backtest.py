"""Backtesting (M2.3, P8): replay the price series + results against predictions.

Strategy under test: flat-stake 1 unit on any prediction whose edge at the ENTRY
price clears ``min_edge_pct``. Settlement comes from ``event_results``; CLV compares
entry to the CLOSING price (the last change-point) — the §16.3 gold metric: a
strategy that beats the close has edge even when short-run results wobble.

Entry discipline (no lookahead): the entry price is what you could actually GET at
prediction time — the prevailing change-point at ``predicted_at``, or the first one
after it when the prediction predates every capture. Taking the first-ever captured
price regardless would credit the model with prices it never saw.

Settlement is resolution-aware: results land under whichever book reported them, so
a prediction keyed on another book's event id settles through the shared fixture
(events → fixture_id). Side-relative winners ("home"/"away") translate between the
two books' listing orders via name-token matching — and when orientation can't be
established, the bet stays UNSETTLED rather than guessing (a flipped side corrupts
ROI silently).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import Event, EventResult, OddsSnapshot, Prediction, Price
from sportsdata_agents.data.repository import TenantScope
from sportsdata_agents.operations.resolution.resolver import _side_ok, _tokens, split_sides

_SIDE_WINNERS = ("home", "away", "draw")


async def _settlement_maps(
    session: AsyncSession,
) -> tuple[
    dict[tuple[str, str], uuid.UUID | None],
    dict[str, uuid.UUID | None],
    dict[str, EventResult],
    dict[uuid.UUID, EventResult],
]:
    """Fixture joins + results, loaded once per backtest instead of per prediction."""
    events = (await session.execute(select(Event))).scalars().all()
    fixture_by_pe = {(e.provider, e.external_id): e.fixture_id for e in events}
    fixture_by_ext: dict[str, uuid.UUID | None] = {}
    for e in events:  # ext-id-only fallback; collisions across providers → unusable
        prior = fixture_by_ext.get(e.external_id, e.fixture_id)
        fixture_by_ext[e.external_id] = e.fixture_id if prior == e.fixture_id else None
    results = (
        await session.execute(
            select(EventResult).order_by(EventResult.settled_at.asc().nulls_first())
        )
    ).scalars().all()
    result_by_ext: dict[str, EventResult] = {}
    result_by_fixture: dict[uuid.UUID, EventResult] = {}
    for res in results:  # ascending order: the newest settlement overwrites
        result_by_ext[res.event_external_id] = res
        fixture = fixture_by_pe.get((res.provider, res.event_external_id)) or fixture_by_ext.get(
            res.event_external_id
        )
        if fixture is not None:
            result_by_fixture[fixture] = res
    return fixture_by_pe, fixture_by_ext, result_by_ext, result_by_fixture


async def _event_name_for(
    session: AsyncSession, cache: dict[tuple[str, str], str], provider: str, external_id: str
) -> str:
    """The event name a book published for (provider, external id) — orientation
    evidence for translating side-relative winners between books."""
    key = (provider, external_id)
    if key not in cache:
        stmt = select(func.max(OddsSnapshot.event_name)).where(
            OddsSnapshot.event_external_id == external_id
        )
        if provider:
            stmt = stmt.where(OddsSnapshot.provider == provider)
        cache[key] = str((await session.execute(stmt)).scalar() or "")
    return cache[key]


def _translate_side(winner: str, pred_name: str, result_name: str) -> str | None:
    """A side-relative winner in the RESULT book's frame → the PREDICTION book's
    frame ("home" flips when the books list the teams in opposite order). None when
    either name fails to split or the sides can't be aligned unambiguously."""
    if winner == "draw":
        return "draw"
    pred_sides, result_sides = split_sides(pred_name), split_sides(result_name)
    if not pred_sides or not result_sides:
        return None
    p_home, p_away = _tokens(pred_sides[0]), _tokens(pred_sides[1])
    r_home, r_away = _tokens(result_sides[0]), _tokens(result_sides[1])
    same = _side_ok(p_home, r_home) and _side_ok(p_away, r_away)
    swapped = _side_ok(p_home, r_away) and _side_ok(p_away, r_home)
    if same == swapped:  # neither or both — never guess
        return None
    return winner if same else {"home": "away", "away": "home"}[winner]


async def run_backtest(
    session_factory: async_sessionmaker[AsyncSession],
    scope: TenantScope,
    *,
    model_id: str | None = None,
    min_edge_pct: float = 2.0,
    book: str | None = None,
) -> dict[str, Any]:
    """Replay predictions vs captured prices + results → ROI / hit-rate / CLV / variance."""
    async with session_factory() as session:
        stmt = select(Prediction).where(
            Prediction.tenant_id == scope.tenant_id,
            Prediction.workspace_id == scope.workspace_id,
        )
        if model_id:
            stmt = stmt.where(Prediction.model_id == uuid.UUID(model_id))
        predictions = (await session.execute(stmt)).scalars().all()

        fixture_by_pe, fixture_by_ext, result_by_ext, result_by_fixture = (
            await _settlement_maps(session)
        )
        name_cache: dict[tuple[str, str], str] = {}
        bets: list[dict[str, Any]] = []
        skipped = {"no_price": 0, "no_result": 0, "below_edge": 0}
        for pred in predictions:
            price_stmt = (
                select(Price)
                .where(
                    Price.event_external_id == pred.event_external_id,
                    Price.market == pred.market,
                    Price.selection == pred.selection,
                )
                .order_by(Price.changed_at)
            )
            if book:
                price_stmt = price_stmt.where(Price.book == book)
            series = (await session.execute(price_stmt)).scalars().all()
            if not series:
                skipped["no_price"] += 1
                continue
            winner: str | None = None
            result = result_by_ext.get(pred.event_external_id)
            if result is not None:  # direct hit: same event id, same book frame
                winner = result.winning_selection
            else:  # settle through the shared fixture (result came from another book)
                fixture = fixture_by_pe.get(
                    (pred.provider, pred.event_external_id)
                ) or fixture_by_ext.get(pred.event_external_id)
                result = result_by_fixture.get(fixture) if fixture is not None else None
                if result is not None:
                    if result.winning_selection in _SIDE_WINNERS:
                        pred_name = await _event_name_for(
                            session, name_cache, pred.provider, pred.event_external_id
                        )
                        result_name = await _event_name_for(
                            session, name_cache, result.provider, result.event_external_id
                        )
                        winner = _translate_side(
                            result.winning_selection, pred_name, result_name
                        )
                    else:  # racing saddle numbers, team names — book-independent
                        winner = result.winning_selection
            if winner is None:
                skipped["no_result"] += 1
                continue

            entry_row = series[0]
            if pred.predicted_at is not None:
                prevailing = [r for r in series if r.changed_at <= pred.predicted_at]
                entry_row = prevailing[-1] if prevailing else series[0]
            entry, closing = float(entry_row.odds), float(series[-1].odds)
            prob = float(pred.prob)
            edge_pct = (prob * entry - 1.0) * 100.0
            if edge_pct < min_edge_pct:
                skipped["below_edge"] += 1
                continue
            won = pred.selection == winner
            pnl = (entry - 1.0) if won else -1.0
            bets.append(
                {
                    "event": pred.event_external_id,
                    "selection": pred.selection,
                    "prob": prob,
                    "entry_odds": entry,
                    "closing_odds": closing,
                    "edge_pct": round(edge_pct, 2),
                    "clv_pct": round((entry / closing - 1.0) * 100.0, 2),
                    "won": won,
                    "pnl": round(pnl, 4),
                }
            )

    if not bets:
        return {"bets": 0, "skipped": skipped, "note": "no qualifying bets — nothing to report"}
    pnls = [b["pnl"] for b in bets]
    clvs = [(b["entry_odds"] / b["closing_odds"] - 1.0) * 100.0 for b in bets]
    mean_pnl = sum(pnls) / len(pnls)
    return {
        "bets": len(bets),
        "staked": float(len(bets)),  # flat 1-unit stakes
        "pnl": round(sum(pnls), 4),
        "roi_pct": round(sum(pnls) / len(bets) * 100.0, 2),
        "hit_rate_pct": round(sum(1 for b in bets if b["won"]) / len(bets) * 100.0, 2),
        "avg_clv_pct": round(sum(clvs) / len(clvs), 2),
        "pnl_variance": round(sum((p - mean_pnl) ** 2 for p in pnls) / len(pnls), 4),
        "min_edge_pct": min_edge_pct,
        "skipped": skipped,
        "per_bet": bets,
    }
