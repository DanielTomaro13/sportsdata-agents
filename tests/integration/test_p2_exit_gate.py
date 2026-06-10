"""🚪 P2 EXIT GATE: the end-to-end quant loop on one warehouse —
ingest → model → value → backtest → eval, every stage the real implementation."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.repository import TenantScope
from sportsdata_agents.evals import gate_against_baseline, load_baseline, run_offline_evals
from sportsdata_agents.operations.ingestion import ingest_once
from sportsdata_agents.quant.metrics import calibration_report
from sportsdata_agents.quant.value import find_value
from sportsdata_agents.tools.quant import quant_tools

pytestmark = pytest.mark.integration

SCOPE = TenantScope("t", "w")


def _nba_payload(home_odds: str, away_odds: str) -> dict[str, Any]:
    return {
        "games": [
            {
                "gameId": "G1",
                "markets": [
                    {
                        "name": "2way",
                        "books": [
                            {
                                "name": "B",
                                "outcomes": [
                                    {"type": "home", "odds": home_odds},
                                    {"type": "away", "odds": away_odds},
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }


class TwoCaptureManager:
    """The real nba feed, two scheduled captures apart: entry → closing prices."""

    def __init__(self) -> None:
        self.captures = [_nba_payload("2.10", "1.80"), _nba_payload("1.90", "1.95")]
        self.n = 0

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        assert name == "nba_odds_today"
        payload = self.captures[min(self.n, 1)]
        self.n += 1
        return payload


async def test_p2_quant_loop_end_to_end(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    # ── 1. INGEST the opening market ────────────────────────────────────────
    # (an inline feed: the registry no longer carries the CDN aggregator, but its
    # normalizer remains the simplest fixture-friendly shape for the loop test)
    from sportsdata_agents.operations.ingestion import Feed, normalize_nba_odds

    manager = TwoCaptureManager()
    feed = Feed(name="nba_odds", tool="nba_odds_today", mcp_groups=("nba.public.cdn",),
                normalizer=normalize_nba_odds)
    r1 = await ingest_once(manager, db_sessionmaker, [feed])
    assert r1["nba_odds"] == {"ok": True, "snapshots": 2, "price_changes": 2}  # first sightings

    tools = {t.name: t for t in quant_tools(db_sessionmaker, SCOPE)}

    # ── 2. MODEL: calibrate on holdout, persist WITH the record, predict ───
    # (prediction happens AFTER the opening capture — entry discipline is real)
    calib = calibration_report(
        [{"prob": 0.7, "outcome": 1}, {"prob": 0.7, "outcome": 1},
         {"prob": 0.7, "outcome": 0}, {"prob": 0.3, "outcome": 0}]
    )
    assert calib["brier"] == pytest.approx(0.19)
    saved = await tools["save_model"].execute({"name": "h2h", "sport": "nba", "calibration": calib})
    await tools["record_predictions"].execute({
        "model_id": saved["model_id"],
        "predictions": [
            {"event_external_id": "G1", "market": "2way", "selection": "home", "prob": 0.60,
             "provider": "nba_cdn"},
            {"event_external_id": "G1", "market": "2way", "selection": "away", "prob": 0.40,
             "provider": "nba_cdn"},
        ],
    })

    # ── the market moves after the prediction: the closing capture ─────────
    r2 = await ingest_once(manager, db_sessionmaker, [feed])
    assert r2["nba_odds"] == {"ok": True, "snapshots": 2, "price_changes": 2}  # both moved

    # ── 3. VALUE: model vs the ENTRY market → the +EV alert ───────────────
    movement = await tools["query_line_movement"].execute({"event_external_id": "G1"})
    first: dict[str, float] = {}
    for m in movement["movement"]:  # oldest first → the first row per selection is entry
        first.setdefault(m["selection"], m["odds"])
    assert first == {"home": 2.10, "away": 1.80}
    value = find_value(
        [{"name": "home", "odds": first["home"]}, {"name": "away", "odds": first["away"]}],
        [{"name": "home", "prob": 0.60}, {"name": "away", "prob": 0.40}],
        min_edge_pct=5.0,
    )
    assert value["value"] == ["home"]  # 0.6 x 2.10 - 1 = +26% edge — the computed alert
    home_sel = next(s for s in value["selections"] if s["name"] == "home")
    assert home_sel["edge_pct"] == pytest.approx(26.0)

    # ── 4. BACKTEST: settle and replay → CLV > 0 ───────────────────────────
    await tools["record_result"].execute(
        {"event_external_id": "G1", "winning_selection": "home", "sport": "nba", "provider": "nba_cdn"}
    )
    report = await tools["run_backtest"].execute({"model_id": saved["model_id"], "min_edge_pct": 5.0})
    assert report["bets"] == 1  # home qualified; away's -28% edge was skipped
    assert report["skipped"]["below_edge"] == 1
    assert report["pnl"] == pytest.approx(1.10)  # entry 2.10, home won
    assert report["avg_clv_pct"] == pytest.approx(10.53, abs=0.01)  # 2.10 vs the 1.90 close
    assert report["avg_clv_pct"] > 0

    # ── 5. EVAL: the quality gate stays green over the goldens ────────────
    assert gate_against_baseline(await run_offline_evals(), load_baseline()) == []
