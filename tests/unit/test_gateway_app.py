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
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as c:
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
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as c:
        r = await c.get("/healthz")
    assert r.status_code == 503 and r.json()["ok"] is False


async def test_unknown_task_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/tasks/nope")).status_code == 404


async def test_conversation_route_maps_to_message(client: httpx.AsyncClient) -> None:
    r = await client.post("/conversations/slack-thread-1/message", json={"text": "hi"})
    assert r.status_code == 200 and r.json()["answer"] == "the answer"


class FakeConvStore:
    def __init__(self) -> None:
        self.appended: list[tuple[str, str, str]] = []

    async def context_for(self, key: str) -> str | None:
        return "user: earlier question\nassistant: earlier answer" if key == "slack-T1" else None

    async def append_turn(self, key: str, user_text: str, answer_text: str) -> None:
        self.appended.append((key, user_text, answer_text))


async def test_conversation_threads_context_and_persists_turn() -> None:
    fake = FakeSession()
    store = FakeConvStore()
    app = create_app(session=fake, conversation_store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as c:
        r = await c.post("/conversations/slack-T1/message", json={"text": "and away games?"})
        assert r.status_code == 200
        # the team saw prior turns + the new question
        assert "earlier answer" in fake.prompts[0]
        assert fake.prompts[0].rstrip().endswith("and away games?")
        # the turn was stored RAW (no context prefix in the journal)
        assert store.appended == [("slack-T1", "and away games?", "the answer")]

        # a fresh thread is stateless
        await c.post("/conversations/slack-NEW/message", json={"text": "hello"})
        assert fake.prompts[1] == "hello"


async def test_artifacts_ride_message_out() -> None:
    class ArtifactSession(FakeSession):
        async def run(self, prompt: str, *, recorder: Any = None) -> RunResult:
            res = await super().run(prompt, recorder=recorder)
            res.artifacts = ["artifacts/abc-chart.png"]
            return res

    app = create_app(session=ArtifactSession())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as c:
        r = await c.post("/message", json={"text": "chart it"})
    assert r.json()["artifacts"] == ["artifacts/abc-chart.png"]


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


async def test_account_returns_tier_and_upgrade_url(client: httpx.AsyncClient) -> None:
    acct = (await client.get("/account")).json()
    assert acct["tier"] and "upgrade_url" in acct
    assert {"mcp_quota", "chat_ui", "full_app", "agents", "addons"} <= set(acct)


async def test_activate_validates_input() -> None:
    app = create_app(session=FakeSession())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        assert (await c.post("/account/activate", json={})).status_code == 422  # no key
        assert (await c.post("/account/activate", json={"key": "not.a.licence"})).status_code == 400


async def test_activate_a_valid_key_upgrades_the_plan(monkeypatch: Any, tmp_path: Any) -> None:
    """The self-serve upgrade: paste a verified key → the account flips tier."""
    import sportsdata_agents.secrets as secrets
    from sportsdata_agents.licensing import license as lic

    priv, pub = lic.generate_keypair()
    monkeypatch.setattr(lic, "LICENSE_PUBLIC_KEY_B64", pub)  # product build with a baked key
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SPORTSDATA_LICENSE", raising=False)
    monkeypatch.setattr(secrets, "_keyring", lambda: None)  # no keychain → license.key fallback

    token = lic.issue_license(priv, tier="pro", issued_to="buyer@x.com", days=30)
    app = create_app(session=FakeSession())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        r = await c.post("/account/activate", json={"key": token})
        assert r.status_code == 200
        body = r.json()
        assert body["issued_to"] == "buyer@x.com" and body["account"]["tier"] == "pro"
        # it persisted: a fresh /account read reflects the new tier
        assert (await c.get("/account")).json()["tier"] == "pro"


async def test_skills_endpoints_list_and_prune(monkeypatch: Any, tmp_path: Any) -> None:
    """The UI's learned-skills panel: GET /skills shows what the generalist grew,
    POST /skills/remove prunes one, built-ins are protected."""
    from sportsdata_agents.tools import skillsmith

    monkeypatch.setenv("SPORTSDATA_AGENTS_DATA_DIR", str(tmp_path))
    await skillsmith.create_skill(
        {"name": "grown", "description": "d", "triggers": ["t"], "body": "b"})

    app = create_app(session=FakeSession())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        listed = (await c.get("/skills")).json()["skills"]
        mine = [s for s in listed if s["source"] == "user"]
        assert [s["name"] for s in mine] == ["grown"] and mine[0]["recalls"] == 0

        assert (await c.post("/skills/remove", json={"name": "vig_removal"})).status_code == 400
        ok = await c.post("/skills/remove", json={"name": "grown"})
        assert ok.status_code == 200 and ok.json()["removed"] is True
        listed = (await c.get("/skills")).json()["skills"]
        assert not [s for s in listed if s["source"] == "user"]


async def test_operator_panel_is_404_for_customers_and_live_for_the_operator(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """The operator console exists ONLY on the operator's deployment: customers'
    installs 404 the routes (the panel doesn't exist for them)."""
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/w.db")
    import sportsdata_agents.app.wizard as wizard

    monkeypatch.setattr(wizard, "configured_provider", lambda: None)
    app = create_app(session=FakeSession())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        # customer install: the panel does not exist
        monkeypatch.delenv("SPORTSDATA_OPERATOR", raising=False)
        assert (await c.get("/operator/overview")).status_code == 404
        assert (await c.post("/operator/budget", json={"cap_usd": 5})).status_code == 404

        # the operator's deployment: full payload + budget round-trip
        monkeypatch.setenv("SPORTSDATA_OPERATOR", "1")
        r = await c.get("/operator/overview")
        assert r.status_code == 200
        body = r.json()
        assert {"preflight", "costs", "budget", "ops"} <= set(body)
        assert body["preflight"]["checks"] and "jobs" in body["ops"]

        ok = await c.post("/operator/budget", json={"cap_usd": 25, "period": "weekly"})
        assert ok.status_code == 200 and ok.json()["budget"]["cap_usd"] == 25.0
        assert (await c.post("/operator/budget", json={"cap_usd": -1})).status_code == 422
        # the new budget shows up in the overview
        assert (await c.get("/operator/overview")).json()["budget"]["period"] == "weekly"


async def test_operator_actions_gated_and_validated(monkeypatch: Any, tmp_path: Any) -> None:
    """The operator action triggers are operator-only (404 for customers); run-ops
    validates the agent against the ops roster and never spawns on bad input."""
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/w.db")
    import sportsdata_agents.app.wizard as wizard

    monkeypatch.setattr(wizard, "configured_provider", lambda: None)
    app = create_app(session=FakeSession())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        # customer install: the action routes don't exist
        monkeypatch.delenv("SPORTSDATA_OPERATOR", raising=False)
        assert (await c.post("/operator/actions/health")).status_code == 404
        assert (await c.post("/operator/actions/run-ops", json={"agent": "x"})).status_code == 404

        monkeypatch.setenv("SPORTSDATA_OPERATOR", "1")  # operator (dev build: env honoured)
        # the overview lists the ops agents the panel offers
        ov = (await c.get("/operator/overview")).json()
        ops_agents = ov["ops"]["agents"]
        assert ops_agents and "incident_triage" in ops_agents

        # health: stub the deterministic check so the test needn't reach a DB/site
        import sportsdata_agents.operations.health as health_mod

        async def fake_health(_sf: Any) -> dict[str, Any]:
            return {"ok": True, "doctor": {"ok": True, "output": ""},
                    "feeds": {"providers_active_6h": 3, "stale_feeds": [], "disabled_feeds": []},
                    "site": {"ok": True, "latency_ms": 42, "playback_mode": False, "error": None}}

        monkeypatch.setattr(health_mod, "run_health", fake_health)
        hr = await c.post("/operator/actions/health")
        assert hr.status_code == 200 and hr.json()["health"]["ok"] is True

        # run-ops: unknown agent → 422 (with the roster); empty prompt → 422; both never spawn
        bad = await c.post("/operator/actions/run-ops", json={"agent": "not_an_agent", "prompt": "go"})
        assert bad.status_code == 422 and bad.json()["ops_agents"]
        empty = await c.post("/operator/actions/run-ops",
                             json={"agent": ops_agents[0], "prompt": "  "})
        assert empty.status_code == 422

        # valid run: the subprocess is stubbed so nothing actually launches
        import subprocess

        spawned: dict[str, Any] = {}
        monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: spawned.update(argv=argv, kw=kw))
        ok = await c.post("/operator/actions/run-ops",
                          json={"agent": ops_agents[0], "prompt": "run your weekly pass"})
        assert ok.status_code == 200 and ok.json() == {"ok": True, "started": True, "agent": ops_agents[0]}
        assert spawned["argv"][1:4] == ["ops", "run", ops_agents[0]]
        assert spawned["kw"].get("start_new_session") is True


async def test_foreign_host_is_rejected() -> None:
    """DNS-rebinding defense: a request whose Host isn't local is 403'd, but the
    .app launcher's /healthz probe stays open."""
    app = create_app(session=FakeSession())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://evil.example.com") as c:
        assert (await c.post("/message", json={"text": "drive the agent"})).status_code == 403
        assert (await c.get("/healthz")).status_code == 200  # probe exempt


async def test_empty_host_is_rejected() -> None:
    """HTTP/1.1 requires Host and browsers always send it — an empty/absent one is
    not a legitimate local client, so it is refused too."""
    app = create_app(session=FakeSession())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        r = await c.post("/message", json={"text": "x"}, headers={"host": ""})
        assert r.status_code == 403


async def test_gateway_token_gates_mutations(monkeypatch: Any) -> None:
    monkeypatch.setenv("SPORTSDATA_GATEWAY_TOKEN", "s3cret")
    app = create_app(session=FakeSession())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as c:
        assert (await c.post("/message", json={"text": "x"})).status_code == 401  # no token
        ok = await c.post("/message", json={"text": "x"}, headers={"X-Sportsdata-Token": "s3cret"})
        assert ok.status_code == 200
        assert (await c.get("/agents")).status_code == 200  # GET not gated


async def test_demo_node_skips_host_guard() -> None:
    """The public demo node faces the internet by design (its own gate); the
    localhost Host guard must NOT apply or it would 403 real visitors."""
    app = create_app(session=FakeSession(), demo_only=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://sportsdata.example") as c:
        assert (await c.get("/demo/prompts")).status_code == 200  # public, foreign host ok


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
