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
    "engine_fair_prices",
    "engine_health",
    "save_model",
    "record_predictions",
    "list_models",
    "query_line_movement",
    "run_backtest",
    "record_result",
    "export_training_data",
}


def _warehouse_key(market: str, selection: str, line: float | None) -> tuple[str, str] | None:
    """Engine board keys → the warehouse's captured-price convention, so recorded
    predictions actually JOIN price rows (value watch / backtest / CLV match on
    exact market+selection strings). None = no stable convention yet, skip."""
    if market == "h2h":
        return "2way", selection
    if market == "line" and line is not None:
        return "spread", f"{selection} {line:+g}"
    if market == "total" and line is not None:
        return "total", f"{selection} {line:g}"
    if market in ("win", "place"):
        return market, selection
    return None


def quant_tools(session_factory: async_sessionmaker[AsyncSession], scope: TenantScope) -> list[ToolDef]:

    async def engine_fair_prices(args: dict[str, Any]) -> Any:
        """{sport, fixture_id, quotes, record?, provider?} — model fair prices for a
        fixture's board from the configured pricing engine (settings: engine_backend).
        quotes: racing {win_odds:{runner:odds}}; footy {h2h:[home,away],
        total:[line,over,under]}. With record=true the prices are stored as
        predictions under an auto-managed "engine:{sport}" model artifact, so the
        existing value watch, backtest and CLV replay them unchanged — provider is
        then required (footy selections are side-relative). Degrades to a clear
        error when no engine is configured; differences inside each price's
        std_error band are noise, never edge."""
        from sportsdata_agents.quant.engines import EngineUnavailable, resolve_engine

        engine = resolve_engine()
        if engine is None:
            return {
                "error": "no pricing engine configured",
                "hint": "set SPORTSDATA_AGENTS_ENGINE_BACKEND=local (engines package installed) "
                        "or =remote with ENGINE_API_URL/ENGINE_API_KEY",
            }
        sport = str(args["sport"])
        fixture_id = str(args["fixture_id"])
        try:
            board = engine.price_board(sport, fixture_id, dict(args.get("quotes") or {}))
        except (EngineUnavailable, ValueError) as e:
            return {"error": str(e)}
        prices = [
            {"market": b.market, "selection": b.selection, "line": b.line,
             "fair_probability": round(b.fair_probability, 6),
             "fair_odds": round(b.fair_odds, 4) if b.fair_probability > 0 else None,
             "std_error": b.std_error}
            for b in board
        ]
        result: dict[str, Any] = {"sport": sport, "fixture_id": fixture_id,
                                  "prices": prices, "count": len(prices)}
        if not args.get("record"):
            return result
        provider = str(args.get("provider", ""))
        if not provider:
            raise ValueError("record=true needs provider — sides are relative to one book's listing")
        async with session_factory() as session:
            name = f"engine:{sport}"
            artifact = (
                await session.execute(
                    select(ModelArtifact)
                    .where(
                        ModelArtifact.tenant_id == scope.tenant_id,
                        ModelArtifact.workspace_id == scope.workspace_id,
                        ModelArtifact.name == name,
                    )
                    .order_by(ModelArtifact.version.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if artifact is None:
                artifact = ModelArtifact(
                    tenant_id=scope.tenant_id, workspace_id=scope.workspace_id,
                    name=name, version=1, sport=sport, market="board",
                    params={"backend": type(engine).__name__},
                    calibration={"source": "pricing-engine", "measured_by": "replay"},
                    trained_at=dt.datetime.now(dt.UTC),
                )
                session.add(artifact)
                await session.flush()
            now = dt.datetime.now(dt.UTC)
            recorded = 0
            skipped = 0
            for b in board:
                if not 0.0 < b.fair_probability < 1.0:
                    continue  # degenerate corners are not predictions
                key = _warehouse_key(b.market, b.selection, b.line)
                if key is None:
                    skipped += 1  # no stable warehouse convention for this family yet
                    continue
                market, selection = key
                session.add(
                    Prediction(
                        tenant_id=scope.tenant_id, workspace_id=scope.workspace_id,
                        model_id=artifact.id, provider=provider,
                        event_external_id=fixture_id, market=market,
                        selection=selection, prob=Decimal(str(round(b.fair_probability, 5))),
                        predicted_at=now,
                    )
                )
                recorded += 1
            model_id = str(artifact.id)
            await session.commit()
        return {**result, "recorded": recorded, "skipped_unmappable": skipped, "model_id": model_id}

    async def engine_health(args: dict[str, Any]) -> Any:
        """Model-health snapshot: backend status, a timed test price, and 24h
        engine-prediction / model_value-alert counts. A silently wrong engine
        manufactures fake edge — check this before trusting a value board."""
        import time

        from sqlalchemy import func

        from sportsdata_agents.data.models import Alert
        from sportsdata_agents.quant.engines import EngineUnavailable, resolve_engine

        out: dict[str, Any] = {}
        try:
            engine = resolve_engine()
        except (EngineUnavailable, ValueError) as e:
            return {"status": "unavailable", "error": str(e)}
        if engine is None:
            return {"status": "not_configured",
                    "hint": "set SPORTSDATA_AGENTS_ENGINE_BACKEND=local or =remote"}
        started = time.monotonic()
        try:
            board = engine.price_board(
                "afl", "HEALTH-CHECK", {"h2h": [1.80, 2.10], "total": [165.5, 1.9, 1.9]}
            )
            out |= {"status": "ok", "sports": engine.sports(),
                    "test_price_ms": round((time.monotonic() - started) * 1000),
                    "test_markets": len(board)}
        except (EngineUnavailable, ValueError) as e:
            out |= {"status": "degraded", "error": str(e)}
        day_ago = dt.datetime.now(dt.UTC) - dt.timedelta(hours=24)
        async with session_factory() as session:
            predictions_24h = (
                await session.execute(
                    select(func.count()).select_from(Prediction).join(
                        ModelArtifact, Prediction.model_id == ModelArtifact.id
                    ).where(
                        Prediction.tenant_id == scope.tenant_id,
                        Prediction.workspace_id == scope.workspace_id,
                        ModelArtifact.name.startswith("engine:", autoescape=True),
                        Prediction.predicted_at > day_ago,
                    )
                )
            ).scalar_one()
            alerts_24h = (
                await session.execute(
                    select(func.count()).select_from(Alert).where(
                        Alert.kind == "model_value", Alert.created_at > day_ago
                    )
                )
            ).scalar_one()
        return {**out, "engine_predictions_24h": predictions_24h, "model_value_alerts_24h": alerts_24h}

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

    async def export_training_data(args: dict[str, Any]) -> Any:
        """{event_external_ids: [...], market?, book?, filename?} → write a flat
        per-(event, market, selection, book) feature table to the DESK FOLDER and
        return the path. The modelling sandbox (run_python, same machine) can't
        reach the DB — this is the bridge: it reads the file to fit/calibrate.

        Features per row: open/close/min/max odds, n_points (change-points seen),
        drift_pct. The settled outcome (1/0) and winning_selection are joined when
        the result is unambiguous; left blank otherwise (never guessed)."""
        events = [str(e) for e in (args.get("event_external_ids") or []) if str(e).strip()]
        if not events:
            raise ValueError("event_external_ids must be a non-empty list")
        market = args.get("market")
        book = args.get("book")

        # Results loaded once. The ext-id label is used only when every provider
        # that reported this event agrees — a cross-provider disagreement (five
        # books share one numeric id namespace) drops to unlabeled rather than guess.
        async with session_factory() as session:
            result_rows = (
                await session.execute(
                    select(EventResult).where(EventResult.event_external_id.in_(events))
                )
            ).scalars().all()
        winner_by_ext: dict[str, str | None] = {}
        for r in result_rows:
            if r.event_external_id not in winner_by_ext:
                winner_by_ext[r.event_external_id] = r.winning_selection
            elif winner_by_ext[r.event_external_id] != r.winning_selection:
                winner_by_ext[r.event_external_id] = None  # ambiguous → no label

        out_rows: list[dict[str, Any]] = []
        labeled = 0
        for ext in events:
            moves = await line_movement(session_factory, event_external_id=ext, market=market, book=book)
            groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
            for m in moves:  # already ordered by changed_at
                groups.setdefault((m["market"], m["selection"], m["book"]), []).append(m)
            winner = winner_by_ext.get(ext)
            for (mk, sel, bk), series in groups.items():
                odds_vals = [s["odds"] for s in series]
                open_odds, close_odds = series[0]["odds"], series[-1]["odds"]
                outcome: int | str = ""
                if winner is not None:
                    outcome = 1 if str(sel).lower() == str(winner).lower() else 0
                    labeled += 1
                out_rows.append({
                    "event_external_id": ext, "market": mk, "selection": sel, "book": bk,
                    "n_points": len(series),
                    "open_odds": round(open_odds, 4), "close_odds": round(close_odds, 4),
                    "min_odds": round(min(odds_vals), 4), "max_odds": round(max(odds_vals), 4),
                    "drift_pct": round((close_odds - open_odds) / open_odds * 100, 4) if open_odds else "",
                    "first_seen": series[0]["changed_at"], "last_seen": series[-1]["changed_at"],
                    "winning_selection": winner or "",
                    "outcome": outcome,
                })
        if not out_rows:
            raise ValueError("no captured prices for those events — nothing to export")

        from sportsdata_agents.tools.desk import export_csv

        columns = ["event_external_id", "market", "selection", "book", "n_points",
                   "open_odds", "close_odds", "min_odds", "max_odds", "drift_pct",
                   "first_seen", "last_seen", "winning_selection", "outcome"]
        filename = str(args.get("filename") or "").strip() or f"training-{events[0]}.csv"
        saved = await export_csv({"filename": filename, "rows": out_rows, "columns": columns})
        return {**saved, "events": len(events), "labeled": labeled,
                "note": "read this path in run_python (same machine) to fit/calibrate"}

    def _tool(name: str, fn: Any, props: dict[str, Any], required: list[str]) -> ToolDef:
        return ToolDef(
            name=name,
            description=(fn.__doc__ or name).strip().splitlines()[0],
            parameters={"type": "object", "properties": props, "required": required},
            execute=fn,
        )

    return [
        _tool("engine_health", engine_health, {}, []),
        _tool(
            "engine_fair_prices",
            engine_fair_prices,
            {
                "sport": {"type": "string", "description": "Engine sport: racing | afl | rugby_league | rugby_union"},
                "fixture_id": {"type": "string"},
                "quotes": {
                    "type": "object",
                    "description": "racing {win_odds:{runner:odds}}; footy {h2h:[home,away], total:[line,over,under]}",
                },
                "record": {"type": "boolean", "description": "Store prices as predictions (CLV/backtest replay them)"},
                "provider": {"type": "string", "description": "Required with record: whose listing sides refer to"},
            },
            ["sport", "fixture_id", "quotes"],
        ),
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
        _tool(
            "export_training_data",
            export_training_data,
            {
                "event_external_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Events to export the captured price history for",
                },
                "market": {"type": "string", "description": "Optional market filter, e.g. 'h2h'"},
                "book": {"type": "string", "description": "Optional bookmaker filter"},
                "filename": {"type": "string", "description": "Desk-folder CSV name (default training-<event>.csv)"},
            },
            ["event_external_ids"],
        ),
    ]
