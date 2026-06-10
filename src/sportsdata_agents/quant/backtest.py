"""Backtesting (M2.3, P8): replay the price series + results against predictions.

Strategy under test: flat-stake 1 unit on any prediction whose edge at the ENTRY
price (the first change-point we captured) clears ``min_edge_pct``. Settlement comes
from ``event_results``; CLV compares entry to the CLOSING price (the last
change-point) — the §16.3 gold metric: a strategy that beats the close has edge
even when short-run results wobble.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import EventResult, Prediction, Price
from sportsdata_agents.data.repository import TenantScope


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
            result = (
                await session.execute(
                    select(EventResult).where(EventResult.event_external_id == pred.event_external_id)
                )
            ).scalar_one_or_none()
            if result is None:
                skipped["no_result"] += 1
                continue

            entry, closing = float(series[0].odds), float(series[-1].odds)
            prob = float(pred.prob)
            edge_pct = (prob * entry - 1.0) * 100.0
            if edge_pct < min_edge_pct:
                skipped["below_edge"] += 1
                continue
            won = pred.selection == result.winning_selection
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
