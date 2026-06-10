"""Bet-tracking + risk tools (M1.4) — DB-backed, advisory-only (§14).

These tools LOG and ANALYSE the user's own bets; nothing here places anything.
They need a database, so they're built per-session by :func:`tracking_tools`
(a ToolDef factory bound to a sessionmaker + tenant scope) and handed to runtimes
as ``extra_tools`` — the spec still grants them by name like any native tool.

CLV (closing-line value): (your_odds / closing_odds - 1) — positive means you beat
the close, the strongest known predictor of long-term edge. Settling with
``closing_odds`` is what makes the CLV report possible.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.agents.harness import ToolDef
from sportsdata_agents.data.models import Performance, TrackedBet
from sportsdata_agents.data.repository import TenantScope

DEFAULT_EXPOSURE_CAP_PCT = 5.0  # max single recommendation, % of bankroll
DEFAULT_TOTAL_EXPOSURE_CAP_PCT = 25.0  # max total open exposure, % of bankroll


def tracking_tools(session_factory: async_sessionmaker[AsyncSession], scope: TenantScope) -> list[ToolDef]:
    """The tracking/risk toolset, bound to a database + tenant scope."""

    def _scoped(stmt: Any) -> Any:
        return stmt.where(
            TrackedBet.tenant_id == scope.tenant_id,
            TrackedBet.workspace_id == scope.workspace_id,
        )

    async def log_bet(args: dict[str, Any]) -> Any:
        """Record a bet the USER says they placed. Advisory invariant: this writes a
        journal row — it does not and cannot place anything."""
        stake = Decimal(str(args["amount"]))
        odds = float(args["odds"])
        if stake <= 0 or odds < 1.01:
            raise ValueError("amount must be > 0 and odds >= 1.01")
        async with session_factory() as session:
            bet = TrackedBet(
                tenant_id=scope.tenant_id,
                workspace_id=scope.workspace_id,
                selection=str(args["selection"]),
                book=str(args.get("book", "?")),
                stake=stake,
                odds=Decimal(str(odds)),
                placed_at=dt.datetime.now(dt.UTC),
                status="open",
            )
            session.add(bet)
            await session.flush()
            bet_id = str(bet.id)
            await session.commit()
        return {"bet_id": bet_id, "status": "open", "selection": args["selection"], "odds": odds}

    async def settle_bet(args: dict[str, Any]) -> Any:
        """{bet_id, result: won|lost|void, closing_odds?} → P&L; closing_odds enables CLV."""
        result = str(args["result"]).lower()
        if result not in ("won", "lost", "void"):
            raise ValueError("result must be won, lost or void")
        if args.get("closing_odds") is not None and float(args["closing_odds"]) < 1.01:
            raise ValueError("closing_odds must be >= 1.01 (decimal odds)")
        async with session_factory() as session:
            bet = await session.get(TrackedBet, uuid.UUID(str(args["bet_id"])))
            if bet is None or bet.tenant_id != scope.tenant_id or bet.workspace_id != scope.workspace_id:
                raise ValueError("unknown bet_id for this workspace")
            if bet.status != "open":
                raise ValueError("bet already settled")
            if result == "won":
                pnl = bet.stake * (bet.odds - 1)
            elif result == "lost":
                pnl = -bet.stake
            else:
                pnl = Decimal("0")
            # The result IS the status (open → won|lost|void): hit-rate needs to know
            # voids from losses, and "show my voided bets" must stay answerable.
            bet.status = result
            bet.result_pnl = pnl
            if args.get("closing_odds") is not None:
                bet.closing_odds = Decimal(str(args["closing_odds"]))
            await session.commit()
            clv = None
            if bet.closing_odds:
                clv = round((float(bet.odds) / float(bet.closing_odds) - 1) * 100, 2)
        return {"bet_id": str(args["bet_id"]), "result": result, "pnl": float(pnl), "clv_pct": clv}

    async def list_bets(args: dict[str, Any]) -> Any:
        """{status?: open|settled|won|lost|void} → the journal ('settled' = any outcome)."""
        async with session_factory() as session:
            stmt = _scoped(select(TrackedBet)).order_by(TrackedBet.placed_at)
            if args.get("status"):
                status = str(args["status"])
                if status == "settled":
                    stmt = stmt.where(TrackedBet.status != "open")
                else:
                    stmt = stmt.where(TrackedBet.status == status)
            bets = (await session.execute(stmt)).scalars().all()
        return {
            "bets": [
                {
                    "bet_id": str(b.id),
                    "selection": b.selection,
                    "book": b.book,
                    "amount": float(b.stake),
                    "odds": float(b.odds),
                    "status": b.status,
                    "pnl": float(b.result_pnl) if b.result_pnl is not None else None,
                }
                for b in bets
            ]
        }

    async def performance_report(args: dict[str, Any]) -> Any:
        """ROI / P&L / hit-rate / average CLV over settled bets; upserts the all-time
        `performance` row (one per workspace until real windows land, P2)."""
        async with session_factory() as session:
            settled = (
                (await session.execute(_scoped(select(TrackedBet)).where(TrackedBet.status != "open")))
                .scalars()
                .all()
            )
            if not settled:
                return {"settled": 0, "note": "no settled bets yet"}
            staked = sum((b.stake for b in settled), Decimal("0"))
            pnl = sum((b.result_pnl or Decimal("0") for b in settled), Decimal("0"))
            wins = sum(1 for b in settled if b.status == "won")
            # Hit-rate counts DECIDED bets only: voids return the stake and say
            # nothing about picking ability.
            decided = sum(1 for b in settled if b.status in ("won", "lost"))
            clvs = [
                (float(b.odds) / float(b.closing_odds) - 1) * 100
                for b in settled
                if b.closing_odds and float(b.closing_odds) >= 1.01
            ]
            roi = float(pnl / staked) if staked else 0.0
            report = {
                "settled": len(settled),
                "staked": float(staked),
                "pnl": float(pnl),
                "roi_pct": round(roi * 100, 2),
                "hit_rate_pct": round(wins / decided * 100, 2) if decided else None,
                "avg_clv_pct": round(sum(clvs) / len(clvs), 2) if clvs else None,
                "clv_sample": len(clvs),
            }
            placed_times = [b.placed_at for b in settled if b.placed_at]
            window_start = min(placed_times) if placed_times else dt.datetime.now(dt.UTC)
            row = (
                await session.execute(
                    select(Performance).where(
                        Performance.tenant_id == scope.tenant_id,
                        Performance.workspace_id == scope.workspace_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = Performance(tenant_id=scope.tenant_id, workspace_id=scope.workspace_id)
                session.add(row)
            row.window_start = window_start
            row.window_end = dt.datetime.now(dt.UTC)
            row.bets_settled = len(settled)
            row.staked = staked
            row.pnl = pnl
            row.roi = Decimal(str(round(roi, 4)))
            row.hit_rate = Decimal(str(round(wins / decided, 4))) if decided else None
            row.avg_clv_pct = Decimal(str(report["avg_clv_pct"])) if clvs else None
            await session.commit()
        return report

    async def exposure_check(args: dict[str, Any]) -> Any:
        """{bankroll, proposed_amount, cap_pct?, total_cap_pct?} → the risk gate (M1.4):
        caps a single recommendation at cap_pct of bankroll AND keeps open exposure
        plus the new stake under total_cap_pct of bankroll."""
        bankroll = float(args["bankroll"])
        proposed = float(args["proposed_amount"])
        cap_pct = float(args.get("cap_pct", DEFAULT_EXPOSURE_CAP_PCT))
        total_cap_pct = float(args.get("total_cap_pct", DEFAULT_TOTAL_EXPOSURE_CAP_PCT))
        if bankroll <= 0 or proposed < 0:
            raise ValueError("bankroll must be > 0 and proposed_amount >= 0")
        async with session_factory() as session:
            open_bets = (
                (await session.execute(_scoped(select(TrackedBet)).where(TrackedBet.status == "open")))
                .scalars()
                .all()
            )
        open_exposure = float(sum((b.stake for b in open_bets), Decimal("0")))
        max_single = bankroll * cap_pct / 100.0
        headroom = max(0.0, bankroll * total_cap_pct / 100.0 - open_exposure)
        capped = min(proposed, max_single, headroom)
        return {
            "bankroll": bankroll,
            "open_exposure": open_exposure,
            "exposure_pct": round(open_exposure / bankroll * 100, 2),
            "max_single_recommendation": round(max_single, 2),
            "total_exposure_headroom": round(headroom, 2),
            "proposed_amount": proposed,
            "allowed": proposed <= min(max_single, headroom),
            "capped_amount": round(capped, 2),
        }

    def _tool(name: str, description: str, properties: dict[str, Any], required: list[str], fn: Any) -> ToolDef:
        return ToolDef(
            name=name,
            description=description,
            parameters={"type": "object", "properties": properties, "required": required},
            execute=fn,
        )

    return [
        _tool(
            "log_bet",
            "Journal a bet the USER placed themselves (advisory platform — never places). "
            "Records selection/book/odds/amount.",
            {
                "selection": {"type": "string"},
                "book": {"type": "string"},
                "odds": {"type": "number", "description": "Decimal odds taken"},
                "amount": {"type": "number", "description": "Amount the user staked (their money, their action)"},
            },
            ["selection", "odds", "amount"],
            log_bet,
        ),
        _tool(
            "settle_bet",
            "Settle a journaled bet (won/lost/void). Provide closing_odds to enable CLV reporting.",
            {
                "bet_id": {"type": "string"},
                "result": {"type": "string", "enum": ["won", "lost", "void"]},
                "closing_odds": {"type": "number", "description": "The closing price — enables CLV"},
            },
            ["bet_id", "result"],
            settle_bet,
        ),
        _tool(
            "list_bets",
            "List journaled bets, optionally filtered by status (open|settled|won|lost|void).",
            {"status": {"type": "string", "enum": ["open", "settled", "won", "lost", "void"]}},
            [],
            list_bets,
        ),
        _tool(
            "performance_report",
            "ROI, P&L, hit-rate (decided bets only) and average CLV over settled bets; "
            "maintains the all-time performance row.",
            {},
            [],
            performance_report,
        ),
        _tool(
            "exposure_check",
            "Risk gate: caps a single recommendation at cap_pct (default 5%) of bankroll and keeps "
            "total open exposure + the new stake under total_cap_pct (default 25%).",
            {
                "bankroll": {"type": "number"},
                "proposed_amount": {"type": "number"},
                "cap_pct": {"type": "number"},
                "total_cap_pct": {"type": "number"},
            },
            ["bankroll", "proposed_amount"],
            exposure_check,
        ),
    ]


TRACKING_TOOL_NAMES = {"log_bet", "settle_bet", "list_bets", "performance_report", "exposure_check"}
