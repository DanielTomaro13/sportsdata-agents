"""M1.1 — the HTTP gateway: sync, async tasks, SSE, tenancy, rate limit (offline)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from sportsdata_agents.agents.harness import RunResult
from sportsdata_agents.gateway.app import RateLimiter, create_app
from sportsdata_agents.gateway.tasks import TaskStore

pytestmark = pytest.mark.unit


class FakeSession:
    """Quacks like TeamSession for the gateway."""

    agent_name = "orchestrator"
    recorder = None

    def __init__(self, *, delay: float = 0.0) -> None:
        self.delay = delay
        self.prompts: list[str] = []

    async def run(self, prompt: str, *, recorder: Any = None) -> RunResult:
        self.prompts.append(prompt)
        if self.delay:
            await asyncio.sleep(self.delay)
        if recorder is not None:  # async path: emit progress like the harness would
            await recorder.on_run_start(run_id=None, parent_run_id=None, agent="orchestrator", task=prompt)
            await recorder.on_tool_call(run_id=None, tool="stats_specialist", arguments={}, ok=True, latency_ms=5)
            await recorder.on_run_end(run_id=None, agent="orchestrator", status="ok", cost_usd=0.01, latency_ms=9)
        return RunResult(output="the answer", stop_reason="done", steps=2, tool_call_count=1,
                         cost_usd=0.0123, verified=True)


@pytest.fixture
async def client():
    app = create_app(session=FakeSession())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as c:
        yield c


async def test_healthz_and_agents(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz")).json() == {"ok": True, "agent": "orchestrator"}
    agents = (await client.get("/agents")).json()
    assert {"orchestrator", "odds_specialist", "stats_specialist"} <= set(agents)


async def test_sync_message(client: httpx.AsyncClient) -> None:
    r = await client.post("/message", json={"text": "who won?"})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "the answer"
    assert body["stop_reason"] == "done" and body["verified"] is True
    assert body["cost_usd"] == pytest.approx(0.0123)


async def test_async_task_lifecycle_and_sse(client: httpx.AsyncClient) -> None:
    r = await client.post("/message?mode=async", json={"text": "long job"})
    task_id = r.json()["task_id"]
    assert r.json()["state"] in ("queued", "running")

    # SSE: progress events then the end marker
    events: list[dict[str, Any]] = []
    async with client.stream("GET", f"/tasks/{task_id}/events") as stream:
        async for line in stream.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
                if events[-1].get("event") == "end":
                    break
    kinds = [e["event"] for e in events]
    assert "run_start" in kinds and "tool_call" in kinds and kinds[-1] == "end"

    status = (await client.get(f"/tasks/{task_id}")).json()
    assert status["state"] == "done"
    assert status["result"]["answer"] == "the answer"


async def test_sse_late_join_gets_end_not_a_hang(client: httpx.AsyncClient) -> None:
    """Connecting after the task finished (or reconnecting after the end marker was
    consumed) must terminate immediately, not idle on keepalives."""
    r = await client.post("/message?mode=async", json={"text": "quick"})
    task_id = r.json()["task_id"]
    for _ in range(100):
        if (await client.get(f"/tasks/{task_id}")).json()["state"] == "done":
            break
        await asyncio.sleep(0.01)

    for _ in range(2):  # first consumer drains; second still terminates cleanly
        events = []
        async with client.stream("GET", f"/tasks/{task_id}/events") as stream:
            async for line in stream.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
                    if events[-1].get("event") == "end":
                        break
        assert events[-1]["event"] == "end"


async def test_healthz_503_before_session_ready() -> None:
    app = create_app(session=None)  # no lifespan run → no session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.get("/healthz")
    assert r.status_code == 503 and r.json()["ok"] is False


async def test_unknown_task_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/tasks/nope")).status_code == 404


async def test_conversation_route_maps_to_message(client: httpx.AsyncClient) -> None:
    r = await client.post("/conversations/slack-thread-1/message", json={"text": "hi"})
    assert r.status_code == 200 and r.json()["answer"] == "the answer"


async def test_agent_mismatch_rejected(client: httpx.AsyncClient) -> None:
    r = await client.post("/message", json={"text": "x", "agent": "odds_specialist"})
    assert r.status_code == 400


async def test_tenant_headers_resolve(client: httpx.AsyncClient) -> None:
    r = await client.post("/message", json={"text": "x"}, headers={"X-Tenant-Id": "acme"})
    assert r.status_code == 200  # resolution is exercised; scoping is enforced downstream


def test_rate_limiter_trips() -> None:
    rl = RateLimiter(per_minute=3)
    for _ in range(3):
        rl.check("t1")
    with pytest.raises(Exception, match=r"429|rate limit"):
        rl.check("t1")
    rl.check("t2")  # other tenants unaffected


async def test_task_store_error_surfaced() -> None:
    store = TaskStore()

    async def boom(_record):
        raise RuntimeError("kaput")

    record = store.submit(lambda r: boom(r))
    for _ in range(50):
        if record.state == "error":
            break
        await asyncio.sleep(0.01)
    assert record.state == "error" and "kaput" in (record.error or "")
