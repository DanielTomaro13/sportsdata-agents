"""M2.2 exit gate: the modelling machinery end-to-end (deterministic, real sandbox).

A scripted run of the modelling spec: run_python computes holdout probabilities in
the REAL sandbox, calibration_metrics scores them, save_model persists the version
WITH its calibration record, record_predictions stores the forward picks, and the
typed answer quotes only computed numbers (grounding verified). LLM-quality grading
of live modelling requests belongs to the M2.4 eval harness.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import ModelArtifact, Prediction
from sportsdata_agents.data.repository import TenantScope
from sportsdata_agents.quant.metrics import log_loss
from sportsdata_agents.tools.quant import quant_tools

pytestmark = pytest.mark.integration

SCOPE = TenantScope("t", "w")

HOLDOUT_PAIRS = [
    {"prob": 0.7, "outcome": 1},
    {"prob": 0.7, "outcome": 1},
    {"prob": 0.7, "outcome": 0},
    {"prob": 0.3, "outcome": 0},
]
EXPECTED_BRIER = 0.19  # (0.09 + 0.09 + 0.49 + 0.09) / 4
EXPECTED_LOG_LOSS = round(log_loss([(0.7, 1), (0.7, 1), (0.7, 0), (0.3, 0)]), 6)

TRAIN_CODE = (
    "outcomes = [1, 1, 0, 1, 0, 1, 1, 0]\n"
    "base_rate = sum(outcomes[:4]) / 4  # train slice\n"
    "holdout = [(0.7, 1), (0.7, 1), (0.7, 0), (0.3, 0)]\n"
    "print('holdout_pairs=' + repr(holdout))\n"
    "print(f'base_rate={base_rate}')\n"
)


class ModellingScript:
    """Scripted provider: train → score → persist → predict → typed final answer."""

    def __init__(self) -> None:
        self.step = 0

    async def complete(self, messages: list[dict[str, Any]], **kw: Any) -> Any:
        from sportsdata_agents.models.gateway import ModelReply, ToolCallRequest

        if kw.get("budget"):
            kw["budget"].charge(0.001)

        def tool(name: str, args: dict[str, Any]) -> Any:
            return ModelReply(text="", model="f", tokens_in=50, tokens_out=10, cost_usd=0.001,
                              tool_calls=(ToolCallRequest(id=f"c{self.step}", name=name, arguments=args),))

        self.step += 1
        if self.step == 1:
            return tool("run_python", {"code": TRAIN_CODE})
        if self.step == 2:
            return tool("calibration_metrics", {"pairs": HOLDOUT_PAIRS})
        if self.step == 3:
            calib = json.loads(str(messages[-1]["content"]))  # the metrics tool's result
            return tool("save_model", {
                "name": "nba_totals_baseline",
                "sport": "nba",
                "market": "total",
                "params": {"method": "base_rate", "train_n": 4},
                "calibration": calib,
            })
        if self.step == 4:
            saved = json.loads(str(messages[-1]["content"]))
            return tool("record_predictions", {
                "model_id": saved["model_id"],
                "predictions": [
                    {"event_external_id": "G9", "selection": "over 220.5", "prob": 0.7, "market": "total"},
                    {"event_external_id": "G10", "selection": "over 215.0", "prob": 0.3, "market": "total"},
                ],
            })
        return tool("final_answer", {
            "answer": f"Saved nba_totals_baseline v1: holdout Brier {EXPECTED_BRIER}, "
                      f"log-loss {EXPECTED_LOG_LOSS}, on 4 holdout games. 2 predictions recorded.",
            "facts": [{"claim": "holdout brier", "value": str(EXPECTED_BRIER), "source": "calibration_metrics"}],
            "sources": ["run_python", "calibration_metrics", "save_model"],
        })


async def test_modelling_machinery_calibrates_and_persists(
    db_sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # run_python artifacts land in the test dir
    from sportsdata_agents.agents.loader import load_builtin_specs
    from sportsdata_agents.agents.runtime import AgentRuntime
    from sportsdata_agents.agents.spec import ToolsSpec
    from sportsdata_agents.workspace import Workspace

    spec = load_builtin_specs()["modelling"].model_copy(
        update={  # machinery test: no MCP, the session tools come from extras
            "tools": ToolsSpec(
                native=["run_python", "calibration_metrics", "save_model", "record_predictions",
                        "list_models", "query_line_movement"]
            )
        }
    )
    async with AgentRuntime(
        spec,
        provider=ModellingScript(),
        workspace=Workspace(tenant_id="t", workspace_id="w"),
        extra_tools=quant_tools(db_sessionmaker, SCOPE),
    ) as rt:
        res = await rt.run("build and calibrate a totals model from recent games")

    assert res.stop_reason == "done"
    assert res.verified is True  # every quoted number exists in tool evidence
    assert res.parsed is not None and str(EXPECTED_BRIER) in res.parsed.answer

    async with db_sessionmaker() as s:
        model = (await s.execute(select(ModelArtifact))).scalar_one()
        assert model.name == "nba_totals_baseline" and model.version == 1
        assert model.calibration == {"brier": EXPECTED_BRIER, "log_loss": EXPECTED_LOG_LOSS, "n": 4}
        assert model.tenant_id == "t"
        preds = (await s.execute(select(Prediction))).scalars().all()
        assert len(preds) == 2 and {float(p.prob) for p in preds} == {0.7, 0.3}
        assert all(p.model_id == model.id for p in preds)


async def test_save_model_refuses_uncalibrated(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    tools = {t.name: t for t in quant_tools(db_sessionmaker, SCOPE)}
    with pytest.raises(ValueError, match="calibration"):
        await tools["save_model"].execute({"name": "vibes_model", "calibration": {}})


async def test_model_versions_increment_and_predictions_scope(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tools = {t.name: t for t in quant_tools(db_sessionmaker, SCOPE)}
    calib = {"brier": 0.2, "log_loss": 0.6, "n": 10}
    v1 = await tools["save_model"].execute({"name": "m", "calibration": calib})
    v2 = await tools["save_model"].execute({"name": "m", "calibration": calib})
    assert (v1["version"], v2["version"]) == (1, 2)

    other = {t.name: t for t in quant_tools(db_sessionmaker, TenantScope("other", "o"))}
    with pytest.raises(ValueError, match="unknown model_id"):
        await other["record_predictions"].execute({
            "model_id": v1["model_id"],
            "predictions": [{"event_external_id": "G1", "selection": "home", "prob": 0.5}],
        })

    listed = await tools["list_models"].execute({})
    assert [m["version"] for m in listed["models"]] == [1, 2]
