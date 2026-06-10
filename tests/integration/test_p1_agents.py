"""M1.4-M1.6 exit gates: tracking/CLV, risk gate, memory persistence, sandboxed analysis."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import Memory, Performance
from sportsdata_agents.data.repository import TenantScope
from sportsdata_agents.tools.memory import memory_tools
from sportsdata_agents.tools.tracking import tracking_tools

pytestmark = pytest.mark.integration

SCOPE = TenantScope("t", "w")


def _tools(sf: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    return {t.name: t for t in [*tracking_tools(sf, SCOPE), *memory_tools(sf, SCOPE)]}


# ── M1.4 exit gate: log 3 → settle → ROI + CLV ───────────────────────────


async def test_log_three_settle_and_report_roi_clv(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    tools = _tools(db_sessionmaker)
    b1 = await tools["log_bet"].execute({"selection": "Bulldogs H2H", "book": "sportsbet", "odds": 1.72, "amount": 50})
    b2 = await tools["log_bet"].execute({"selection": "Crows H2H", "book": "pointsbet", "odds": 2.13, "amount": 25})
    b3 = await tools["log_bet"].execute({"selection": "Swans line", "book": "tab", "odds": 1.90, "amount": 25})

    # settle: 1 win (beat the close), 1 loss (worse than close), 1 void
    s1 = await tools["settle_bet"].execute({"bet_id": b1["bet_id"], "result": "won", "closing_odds": 1.60})
    s2 = await tools["settle_bet"].execute({"bet_id": b2["bet_id"], "result": "lost", "closing_odds": 2.30})
    s3 = await tools["settle_bet"].execute({"bet_id": b3["bet_id"], "result": "void"})

    assert s1["pnl"] == pytest.approx(36.0)  # 50 * 0.72
    assert s1["clv_pct"] == pytest.approx(7.5)  # 1.72/1.60 - 1
    assert s2["pnl"] == -25.0
    assert s2["clv_pct"] == pytest.approx(-7.39, abs=0.01)
    assert s3["pnl"] == 0.0

    report = await tools["performance_report"].execute({})
    assert report["settled"] == 3
    assert report["staked"] == 100.0
    assert report["pnl"] == pytest.approx(11.0)
    assert report["roi_pct"] == pytest.approx(11.0)
    assert report["avg_clv_pct"] == pytest.approx((7.5 - 7.39) / 2, abs=0.02)
    assert report["clv_sample"] == 2  # the void had no closing price

    # the performance window row persisted (the M0.3-promised table, in use)
    async with db_sessionmaker() as s:
        row = (await s.execute(select(Performance))).scalar_one()
        assert row.bets_settled == 3 and float(row.pnl) == pytest.approx(11.0)
        assert row.tenant_id == "t"


async def test_settlement_guards(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    tools = _tools(db_sessionmaker)
    bet = await tools["log_bet"].execute({"selection": "X", "odds": 2.0, "amount": 10})
    await tools["settle_bet"].execute({"bet_id": bet["bet_id"], "result": "won"})
    with pytest.raises(ValueError, match="already settled"):
        await tools["settle_bet"].execute({"bet_id": bet["bet_id"], "result": "lost"})
    with pytest.raises(ValueError, match="result must be"):
        await tools["settle_bet"].execute({"bet_id": bet["bet_id"], "result": "push"})


# ── M1.4 exit gate: the risk manager caps a stake ────────────────────────


async def test_exposure_gate_caps_a_recommendation(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    tools = _tools(db_sessionmaker)
    await tools["log_bet"].execute({"selection": "open one", "odds": 2.0, "amount": 40})  # open exposure

    verdict = await tools["exposure_check"].execute({"bankroll": 1000, "proposed_amount": 80})
    assert verdict["allowed"] is False  # 80 > 5% of 1000
    assert verdict["capped_amount"] == 50.0
    assert verdict["open_exposure"] == 40.0

    ok = await tools["exposure_check"].execute({"bankroll": 1000, "proposed_amount": 30})
    assert ok["allowed"] is True and ok["capped_amount"] == 30.0


# ── M1.5 exit gate: memory persists across sessions ─────────────────────


async def test_preference_set_in_one_session_recalled_in_the_next(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # session 1 remembers; a SEPARATE toolset instance (new session) recalls
    session1 = _tools(db_sessionmaker)
    await session1["remember"].execute({"key": "favourite_team", "value": "Western Bulldogs", "kind": "preference"})
    await session1["remember"].execute({"key": "staking_note", "value": "half kelly only", "kind": "note"})

    session2 = _tools(db_sessionmaker)
    out = await session2["recall"].execute({"query": "bulldogs"})
    assert any(m["text"] == "Western Bulldogs" for m in out["memories"])
    out2 = await session2["recall"].execute({"query": "kelly"})
    assert any("half kelly" in m["text"] for m in out2["memories"])

    # upsert: re-remembering a key replaces, not duplicates
    await session2["remember"].execute({"key": "favourite_team", "value": "Sydney Swans"})
    async with db_sessionmaker() as s:
        rows = (await s.execute(select(Memory).where(Memory.key == "favourite_team"))).scalars().all()
        assert len(rows) == 1 and rows[0].value["text"] == "Sydney Swans"


async def test_memory_is_tenant_scoped(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    mine = _tools(db_sessionmaker)
    await mine["remember"].execute({"key": "secret_pref", "value": "mine"})
    other = {t.name: t for t in memory_tools(db_sessionmaker, TenantScope("other", "other"))}
    out = await other["recall"].execute({"query": "secret_pref"})
    assert out["memories"] == []


# ── M1.6 exit gate: chart + grounded commentary through the harness ──────


async def test_data_analysis_chart_end_to_end(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A scripted run of the data_analysis machinery: run_python computes from data,
    saves a chart artifact, and the typed final answer quotes ONLY computed numbers
    (grounding verifies stdout as evidence)."""
    monkeypatch.chdir(tmp_path)
    from sportsdata_agents.agents.loader import load_builtin_specs
    from sportsdata_agents.agents.runtime import AgentRuntime
    from sportsdata_agents.agents.spec import ToolsSpec
    from sportsdata_agents.models.gateway import ModelReply, ToolCallRequest
    from sportsdata_agents.workspace import Workspace

    spec = load_builtin_specs()["data_analysis"].model_copy(
        update={"tools": ToolsSpec(native=["run_python"])}  # no MCP needed for the machinery test
    )
    code = (
        "scores = [88, 95, 102, 91, 99, 105, 97, 110, 93, 100]\n"
        "avg = sum(scores) / len(scores)\n"
        "print(f'avg_last_10={avg}')\n"
        "open('chart.png', 'wb').write(b'PNGFAKE')\n"
    )

    class P:
        calls = 0

        async def complete(self, messages, **kw):  # type: ignore[no-untyped-def]
            P.calls += 1
            if kw.get("budget"):
                kw["budget"].charge(0.001)
            if P.calls == 1:
                return ModelReply(text="", model="f", tokens_in=50, tokens_out=10, cost_usd=0.001,
                                  tool_calls=(ToolCallRequest(id="c", name="run_python", arguments={"code": code}),))
            return ModelReply(text="", model="f", tokens_in=50, tokens_out=10, cost_usd=0.001,
                              tool_calls=(ToolCallRequest(id="f", name="final_answer", arguments={
                                  "answer": "Average over the last 10 games: 98.0 points. Chart saved.",
                                  "facts": [{"claim": "avg last 10", "value": "98.0", "source": "run_python"}],
                                  "sources": ["run_python"],
                              }),))

    async with AgentRuntime(spec, provider=P(), workspace=Workspace(tenant_id="t", workspace_id="w")) as rt:
        res = await rt.run("chart the form over the last 10 games")

    assert res.stop_reason == "done"
    assert res.verified is True  # 98.0 grounded in run_python stdout
    assert res.parsed is not None and "98.0" in res.parsed.answer
    saved = list((tmp_path / "artifacts").glob("*-chart.png"))
    assert len(saved) == 1 and saved[0].read_bytes() == b"PNGFAKE"
