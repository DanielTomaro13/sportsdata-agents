"""Modelling/history session tools (M2.2) — DB-backed via the extra_tools seam.

Same contract as tracking/memory: built per session with a sessionmaker + tenant
scope, granted by name in specs, degrading to the actionable stub when no DB is up.
``query_line_movement`` reads the GLOBAL warehouse (market data is tenant-neutral);
models and predictions are tenant-scoped rows.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.agents.harness import ToolDef
from sportsdata_agents.data.models import EventResult, ModelArtifact, Prediction
from sportsdata_agents.data.repository import TenantScope
from sportsdata_agents.operations.ingestion.store import line_movement

QUANT_TOOL_NAMES = {
    "save_model",
    "record_predictions",
    "list_models",
    "query_line_movement",
    "run_backtest",
    "record_result",
}


def quant_tools(session_factory: async_sessionmaker[AsyncSession], scope: TenantScope) -> list[ToolDef]:
    async def save_model(args: dict[str, Any]) -> Any:
        """{name, sport, market?, params?, calibration{brier,log_loss,n}} → persist a
        model version; calibration metadata is REQUIRED (an uncalibrated model is not
        a model here, §6 Tier-2)."""
        calibration = args.get("calibration") or {}
        if "brier" not in calibration or "log_loss" not in calibration:
            raise ValueError("calibration must include brier and log_loss (run calibration_metrics first)")
        async with session_factory() as session:
            latest = (
                await session.execute(
                    select(ModelArtifact)
                    .where(
                        ModelArtifact.tenant_id == scope.tenant_id,
                        ModelArtifact.workspace_id == scope.workspace_id,
                        ModelArtifact.name == str(args["name"]),
                    )
                    .order_by(ModelArtifact.version.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            row = ModelArtifact(
                tenant_id=scope.tenant_id,
                workspace_id=scope.workspace_id,
                name=str(args["name"]),
                version=(latest.version + 1) if latest else 1,
                sport=str(args.get("sport", "")),
                market=str(args.get("market", "")),
                params=args.get("params") or {},
                calibration=calibration,
                trained_at=dt.datetime.now(dt.UTC),
            )
            session.add(row)
            await session.flush()
            model_id = str(row.id)
            version = row.version
            await session.commit()
        return {"model_id": model_id, "name": args["name"], "version": version, "calibration": calibration}

    async def record_predictions(args: dict[str, Any]) -> Any:
        """{model_id, predictions: [{event_external_id, selection, prob, market?,
        provider?, predicted_at?}]} — predicted_at (ISO) backdates a prediction so
        historical scenarios backtest with honest entry prices (default: now).
        provider is REQUIRED for home/away/draw selections: those are relative to
        one book's listing order, and cross-book settlement can only translate the
        side when it knows whose frame the prediction is in."""
        model_uuid = uuid.UUID(str(args["model_id"]))
        rows = args.get("predictions") or []
        if not rows:
            raise ValueError("predictions must be a non-empty list")
        async with session_factory() as session:
            model = await session.get(ModelArtifact, model_uuid)
            if model is None or model.tenant_id != scope.tenant_id or model.workspace_id != scope.workspace_id:
                raise ValueError("unknown model_id for this workspace")
            now = dt.datetime.now(dt.UTC)
            for r in rows:
                prob = float(r["prob"])
                if not 0.0 <= prob <= 1.0:
                    raise ValueError(f"prob {prob} outside [0, 1]")
                if str(r["selection"]).lower() in ("home", "away", "draw") and not r.get("provider"):
                    raise ValueError(
                        f"selection {r['selection']!r} is side-relative — pass the provider "
                        "whose listing the side refers to (books disagree on home/away order)"
                    )
                predicted_at = (
                    dt.datetime.fromisoformat(str(r["predicted_at"])) if r.get("predicted_at") else now
                )
                session.add(
                    Prediction(
                        tenant_id=scope.tenant_id,
                        workspace_id=scope.workspace_id,
                        model_id=model_uuid,
                        provider=str(r.get("provider", "")),
                        event_external_id=str(r["event_external_id"]),
                        market=str(r.get("market", "")),
                        selection=str(r["selection"]),
                        prob=Decimal(str(round(prob, 5))),
                        predicted_at=predicted_at,
                    )
                )
            await session.commit()
        return {"model_id": str(args["model_id"]), "recorded": len(rows)}

    async def list_models(args: dict[str, Any]) -> Any:
        """The workspace's model versions with calibration metadata."""
        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ModelArtifact)
                        .where(
                            ModelArtifact.tenant_id == scope.tenant_id,
                            ModelArtifact.workspace_id == scope.workspace_id,
                        )
                        .order_by(ModelArtifact.name, ModelArtifact.version)
                    )
                )
                .scalars()
                .all()
            )
        return {
            "models": [
                {
                    "model_id": str(m.id),
                    "name": m.name,
                    "version": m.version,
                    "sport": m.sport,
                    "market": m.market,
                    "calibration": m.calibration,
                }
                for m in rows
            ]
        }

    async def run_backtest(args: dict[str, Any]) -> Any:
        """{model_id?, min_edge_pct?, book?, clv_book?} → replay predictions vs the
        captured price series + results: ROI, hit-rate, average CLV, P&L variance.
        clv_book (e.g. "Pinnacle") benchmarks CLV against that book's close at the
        same fixture instead of the bet book's own close."""
        from sportsdata_agents.quant.backtest import run_backtest as _run

        return await _run(
            session_factory,
            scope,
            model_id=args.get("model_id"),
            min_edge_pct=float(args.get("min_edge_pct", 2.0)),
            book=args.get("book"),
            clv_book=args.get("clv_book"),
        )

    async def record_result(args: dict[str, Any]) -> Any:
        """{event_external_id, winning_selection, sport?, provider?, event_name?} →
        journal a final result into the GLOBAL results table (what backtests settle
        against). Upserts per (provider, event id). For home/away winners, pass
        event_name ("X v Y" as that book lists it) so cross-book settlement can
        translate the side into other books' listing orders."""
        event_id = str(args["event_external_id"])
        provider = str(args.get("provider", ""))
        async with session_factory() as session:
            existing = (
                (await session.execute(select(EventResult).where(
                    EventResult.event_external_id == event_id,
                    EventResult.provider == provider,
                )))
                .scalars()
                .first()
            )
            if existing is not None:
                existing.winning_selection = str(args["winning_selection"])
                existing.settled_at = dt.datetime.now(dt.UTC)
                if args.get("event_name"):
                    existing.meta = {**(existing.meta or {}), "event_name": str(args["event_name"])}
            else:
                session.add(
                    EventResult(
                        provider=provider,
                        sport=str(args.get("sport", "")),
                        event_external_id=event_id,
                        winning_selection=str(args["winning_selection"]),
                        settled_at=dt.datetime.now(dt.UTC),
                        meta={"event_name": str(args["event_name"])} if args.get("event_name") else {},
                    )
                )
            await session.commit()
        return {"recorded": event_id, "winner": args["winning_selection"]}

    async def query_line_movement(args: dict[str, Any]) -> Any:
        """{event_external_id, market?, selection?, book?} → change-point price series
        from the odds warehouse (M2.1)."""
        moves = await line_movement(
            session_factory,
            event_external_id=str(args["event_external_id"]),
            market=args.get("market"),
            selection=args.get("selection"),
            book=args.get("book"),
        )
        return {"event_external_id": args["event_external_id"], "movement": moves}

    def _tool(name: str, fn: Any, props: dict[str, Any], required: list[str]) -> ToolDef:
        return ToolDef(
            name=name,
            description=(fn.__doc__ or name).strip().splitlines()[0],
            parameters={"type": "object", "properties": props, "required": required},
            execute=fn,
        )

    return [
        _tool(
            "save_model",
            save_model,
            {
                "name": {"type": "string"},
                "sport": {"type": "string"},
                "market": {"type": "string"},
                "params": {"type": "object"},
                "calibration": {
                    "type": "object",
                    "description": "From calibration_metrics: {brier, log_loss, n}",
                },
            },
            ["name", "calibration"],
        ),
        _tool(
            "record_predictions",
            record_predictions,
            {
                "model_id": {"type": "string"},
                "predictions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "event_external_id": {"type": "string"},
                            "selection": {"type": "string"},
                            "prob": {"type": "number"},
                            "market": {"type": "string"},
                            "provider": {"type": "string"},
                            "predicted_at": {
                                "type": "string",
                                "description": "ISO timestamp; backdate for historical backtests",
                            },
                        },
                        "required": ["event_external_id", "selection", "prob"],
                    },
                },
            },
            ["model_id", "predictions"],
        ),
        _tool("list_models", list_models, {}, []),
        _tool(
            "run_backtest",
            run_backtest,
            {
                "model_id": {"type": "string"},
                "min_edge_pct": {"type": "number", "description": "Entry-edge threshold (default 2.0)"},
                "book": {"type": "string"},
                "clv_book": {"type": "string",
                             "description": 'CLV benchmark book, e.g. "Pinnacle" (sharp close)'},
            },
            [],
        ),
        _tool(
            "record_result",
            record_result,
            {
                "event_external_id": {"type": "string"},
                "winning_selection": {"type": "string"},
                "sport": {"type": "string"},
                "provider": {"type": "string"},
                "event_name": {"type": "string",
                               "description": '"X v Y" as the recording book lists it '
                                              "(lets home/away results settle other books)"},
            },
            ["event_external_id", "winning_selection"],
        ),
        _tool(
            "query_line_movement",
            query_line_movement,
            {
                "event_external_id": {"type": "string"},
                "market": {"type": "string"},
                "selection": {"type": "string"},
                "book": {"type": "string"},
            },
            ["event_external_id"],
        ),
    ]
