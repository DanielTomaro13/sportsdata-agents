"""Offline eval runner (M2.4): deterministic scores from golden datasets.

Every score is HIGHER-IS-BETTER so the gate is one rule: a score below
``baseline - tolerance`` is a regression. The offline suite needs no model key and
no network — it scores the quant pipeline (calibration, CLV) and the grounding
verifier against committed goldens, so "did this change make the platform worse?"
is answerable in CI on every scheduled run. LLM-quality evals (routing efficiency,
live answer accuracy) are pytest ``-m eval`` cases — scheduled, key-gated, never in
default CI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GOLDEN_DIR = Path(__file__).parent / "golden"
DEFAULT_BASELINE = Path(__file__).parent / "baseline.json"
DEFAULT_TOLERANCE = 0.005  # absolute score drop allowed before the gate trips


@dataclass(frozen=True)
class EvalScore:
    name: str
    score: float  # higher is better, by contract
    details: dict[str, Any]


def _golden(name: str) -> Any:
    return json.loads((GOLDEN_DIR / name).read_text(encoding="utf-8"))


def eval_calibration() -> EvalScore:
    """1 - Brier over the golden holdout predictions (1.0 = oracle, 0.75 = coin flip)."""
    from sportsdata_agents.quant.metrics import calibration_report

    data = _golden("calibration.json")
    report = calibration_report(data["pairs"])
    return EvalScore(
        name="calibration",
        score=round(1.0 - report["brier"], 6),
        details={"brier": report["brier"], "log_loss": report["log_loss"], "n": report["n"]},
    )


async def eval_clv_backtest() -> EvalScore:
    """Average CLV%% the strategy achieves on the golden price history (scaled to a
    0..1-ish score as clv/100 + 0.5 so 'higher is better' holds around zero CLV).
    Runs the REAL backtest math over an in-memory warehouse."""
    import datetime as dt

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from sportsdata_agents.data.base import Base
    from sportsdata_agents.data.models import EventResult
    from sportsdata_agents.data.repository import TenantScope
    from sportsdata_agents.operations.ingestion import PricePoint, record_points
    from sportsdata_agents.quant.backtest import run_backtest
    from sportsdata_agents.tools.quant import quant_tools

    data = _golden("backtest.json")
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    scope = TenantScope("eval", "eval")
    try:
        base = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        for i, capture in enumerate(data["captures"]):
            points = [PricePoint(**{**p, "provider": "golden", "sport": "golden"}) for p in capture]
            await record_points(sf, points, captured_at=base + dt.timedelta(hours=6 * i))
        async with sf() as session:
            for r in data["results"]:
                session.add(EventResult(provider="golden", sport="golden", **r))
            await session.commit()
        tools = {t.name: t for t in quant_tools(sf, scope)}
        saved = await tools["save_model"].execute(
            {"name": "golden", "calibration": {"brier": 0.19, "log_loss": 0.57, "n": 4}}
        )
        await tools["record_predictions"].execute(
            {"model_id": saved["model_id"], "predictions": data["predictions"]}
        )
        report = await run_backtest(sf, scope, min_edge_pct=data["min_edge_pct"])
    finally:
        await engine.dispose()
    clv = float(report.get("avg_clv_pct") or 0.0)
    return EvalScore(
        name="clv_backtest",
        score=round(clv / 100.0 + 0.5, 6),
        details={"avg_clv_pct": clv, "bets": report["bets"], "roi_pct": report.get("roi_pct")},
    )


def eval_grounding() -> EvalScore:
    """Fraction of golden (answer, evidence, expected) cases the verifier judges
    correctly — catches both fabrication misses AND false-positive 'grounded' badges."""
    from sportsdata_agents.agents.grounding import grounding_verifier

    cases = _golden("grounding.json")["cases"]
    correct = 0
    misses: list[str] = []
    for case in cases:
        ok, _ = grounding_verifier(case["answer"], case["evidence"])
        if ok == case["expect_verified"]:
            correct += 1
        else:
            misses.append(case["name"])
    return EvalScore(
        name="grounding",
        score=round(correct / len(cases), 6),
        details={"n": len(cases), "misses": misses},
    )


async def eval_resolution() -> EvalScore:
    """Event resolution on the golden cross-book scenario: four spellings of one
    match join, distinct matches stay apart, ambiguity is skipped never guessed.
    Runs the REAL resolver over an in-memory warehouse."""
    import datetime as dt

    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from sportsdata_agents.data.base import Base
    from sportsdata_agents.data.models import Event, Fixture
    from sportsdata_agents.operations.ingestion import PricePoint, record_points
    from sportsdata_agents.operations.resolution import resolve_events

    data = _golden("resolution.json")
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    try:
        captured = dt.datetime(2026, 6, 11, 7, 0, tzinfo=dt.UTC)
        await record_points(sf, [PricePoint(**p) for p in data["captures"]], captured_at=captured)
        await resolve_events(sf)
        await record_points(sf, [PricePoint(**data["ambiguous_capture"])], captured_at=captured)
        second = await resolve_events(sf)
        async with sf() as session:
            n_fixtures = (await session.execute(select(func.count()).select_from(Fixture))).scalar_one()
            bulldogs = (
                await session.execute(select(Fixture).where(Fixture.name.like("%Bulldogs%")))
            ).scalars().first()
            bulldogs_books = 0
            if bulldogs is not None:
                bulldogs_books = (
                    await session.execute(
                        select(func.count()).select_from(Event).where(Event.fixture_id == bulldogs.id)
                    )
                ).scalar_one()
    finally:
        await engine.dispose()
    expect = data["expect"]
    checks = {
        "fixtures": n_fixtures == expect["fixtures"],
        "bulldogs_books": bulldogs_books == expect["bulldogs_books"],
        "ambiguous_skipped": second["ambiguous"] == expect["ambiguous"] and second["mapped"] == 0,
    }
    return EvalScore(
        name="resolution",
        score=round(sum(checks.values()) / len(checks), 6),
        details={"fixtures": n_fixtures, "bulldogs_books": bulldogs_books,
                 "second_pass": second, "failed": [k for k, v in checks.items() if not v]},
    )


async def run_offline_evals() -> list[EvalScore]:
    return [eval_calibration(), await eval_clv_backtest(), eval_grounding(), await eval_resolution()]


def load_baseline(path: Path | None = None) -> dict[str, float]:
    return {k: float(v) for k, v in json.loads((path or DEFAULT_BASELINE).read_text()).items()}


def gate_against_baseline(
    scores: list[EvalScore], baseline: dict[str, float], *, tolerance: float = DEFAULT_TOLERANCE
) -> list[str]:
    """Regressions (empty = pass): scores below baseline - tolerance, or missing evals
    the baseline knows — silently DROPPING an eval must trip the gate too."""
    by_name = {s.name: s.score for s in scores}
    problems = []
    for name, floor in baseline.items():
        if name not in by_name:
            problems.append(f"{name}: eval missing (baseline {floor})")
        elif by_name[name] < floor - tolerance:
            problems.append(f"{name}: {by_name[name]:.4f} < baseline {floor:.4f} (tolerance {tolerance})")
    return problems
