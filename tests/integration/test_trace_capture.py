"""M4.5 observability: the recorder persists each run's transcript + input task, and
the gateway exposes an agent's activity (/agents/{id}/runs) and a run's full trace
(/runs/{id}) — including the delegation tree."""

from __future__ import annotations

import uuid

import httpx
import pytest

from sportsdata_agents.data.repository import TenantScope
from sportsdata_agents.gateway.app import create_app
from sportsdata_agents.observability.recorder import DbRecorder

pytestmark = pytest.mark.integration


class FakeSession:
    agent_name = "orchestrator"

    def __init__(self, recorder: DbRecorder) -> None:
        self.recorder = recorder

    async def run(self, *a: object, **k: object) -> object:  # pragma: no cover - unused
        raise NotImplementedError


async def test_capture_then_activity_and_trace(db_sessionmaker) -> None:
    rec = DbRecorder(db_sessionmaker, TenantScope("local", "local"))  # endpoint scope = local/local
    rid, crid = uuid.uuid4(), uuid.uuid4()

    await rec.on_run_start(run_id=rid, parent_run_id=None, agent="odds_specialist", task="compare odds")
    await rec.on_tool_call(run_id=rid, tool="sportsbet_markets", arguments={"id": 1}, ok=True, latency_ms=12)
    # a delegated sub-run (the "chat with another agent")
    await rec.on_run_start(run_id=crid, parent_run_id=rid, agent="stats_specialist", task="get the stats")
    await rec.on_run_end(run_id=crid, agent="stats_specialist", status="ok", cost_usd=0.001, latency_ms=5,
                         transcript=[{"role": "user", "content": "get the stats"},
                                     {"role": "assistant", "content": "done"}])
    await rec.on_run_end(run_id=rid, agent="odds_specialist", status="ok", cost_usd=0.01, latency_ms=20,
                         transcript=[{"role": "system", "content": "sys"},
                                     {"role": "user", "content": "compare odds"},
                                     {"role": "assistant", "content": "let me check",
                                      "tool_calls": [{"name": "sportsbet_markets"}]},
                                     {"role": "tool", "content": "odds: 2.1"}])

    app = create_app(session=FakeSession(rec))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as c:
        runs = (await c.get("/agents/odds_specialist/runs")).json()["runs"]
        assert len(runs) == 1
        assert runs[0]["task"] == "compare odds" and runs[0]["status"] == "ok"
        assert runs[0]["is_delegation"] is False

        trace = (await c.get(f"/runs/{rid}")).json()
        assert trace["task"] == "compare odds" and trace["agent"] == "odds_specialist"
        roles = [m["role"] for m in trace["transcript"]]
        assert "system" not in roles and "assistant" in roles and "tool" in roles  # distilled
        assert any(m.get("tools") == ["sportsbet_markets"] for m in trace["transcript"])
        assert [t["tool"] for t in trace["tool_calls"]] == ["sportsbet_markets"]
        # the delegation tree — who it handed work to
        assert [d["agent"] for d in trace["delegations"]] == ["stats_specialist"]
        assert trace["delegations"][0]["task"] == "get the stats"

        assert (await c.get(f"/runs/{uuid.uuid4()}")).status_code == 404
        assert (await c.get("/runs/not-a-uuid")).status_code == 400
