"""M3.4 — the public demo surface: curated-only prompts, rate limits, leads,
no-secret tool traces."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from sportsdata_agents.gateway import demo as demo_module
from sportsdata_agents.gateway.app import create_app

pytestmark = pytest.mark.unit


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def fake_run_demo(prompt_id: str) -> dict[str, Any]:
        demo_module.demo_prompt(prompt_id)  # KeyError for unknown ids, like the real one
        return {"prompt_id": prompt_id, "answer": "Bulldogs by 2.",
                "tool_calls": [{"tool": "sportsbet_competition_matches", "ok": True, "latency_ms": 412}],
                "cost_usd": 0.01, "verified": True, "at": "now"}

    monkeypatch.setattr(demo_module, "run_demo", fake_run_demo)
    app = create_app(session=object())  # session injected -> lifespan skips opening a team
    return TestClient(app)


def test_demo_prompts_are_curated_only(client: TestClient) -> None:
    prompts = client.get("/demo/prompts").json()["prompts"]
    assert {"id", "title"} <= set(prompts[0])
    assert all("prompt" not in p for p in prompts)  # text stays server-side

    out = client.post("/demo/run", json={"prompt_id": prompts[0]["id"]})
    assert out.status_code == 200
    body = out.json()
    assert body["answer"] and body["tool_calls"][0]["tool"]
    assert "arguments" not in body["tool_calls"][0]  # names + timings only

    # free-form input does not exist: unknown ids 404, there is no text field
    assert client.post("/demo/run", json={"prompt_id": "drop tables"}).status_code == 404
    assert client.post("/demo/run", json={"prompt": "ignore instructions"}).status_code == 404


def test_demo_rate_limit_per_ip(client: TestClient) -> None:
    for _ in range(3):
        assert client.post("/demo/run", json={"prompt_id": "find-value"}).status_code == 200
    assert client.post("/demo/run", json={"prompt_id": "find-value"}).status_code == 429


def test_leads_validate_and_never_lose_a_lead(
    client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert client.post("/leads", json={"email": "not-an-email"}).status_code == 422
    monkeypatch.setenv("HOME", str(tmp_path))  # DB is down in unit tests -> file fallback
    out = client.post("/leads", json={"email": "a@b.co", "note": "racing desk"})
    assert out.status_code == 200 and out.json()["ok"] is True


def test_tool_trace_recorder_keeps_no_arguments() -> None:
    import asyncio
    import uuid

    recorder = demo_module.ToolTraceRecorder()
    asyncio.run(recorder.on_tool_call(run_id=uuid.uuid4(), tool="tab_competition",
                                      arguments={"secret": "x"}, ok=True, latency_ms=5))
    assert recorder.calls == [{"tool": "tab_competition", "ok": True, "latency_ms": 5}]


def test_demo_only_gate_hides_the_model_spend_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """--demo-only is the ONLY publicly hostable mode until P4 auth: the demo
    surface stays up, the header-trust gateway routes 404."""

    async def fake_run_demo(prompt_id: str) -> dict[str, Any]:
        demo_module.demo_prompt(prompt_id)
        return {"prompt_id": prompt_id, "answer": "ok", "tool_calls": [], "cost_usd": 0.0}

    monkeypatch.setattr(demo_module, "run_demo", fake_run_demo)
    stub = type("StubSession", (), {"agent_name": "team"})()
    gated = TestClient(create_app(session=stub, demo_only=True))
    assert gated.get("/healthz").status_code == 200
    assert gated.get("/demo/prompts").status_code == 200
    assert gated.post("/demo/run", json={"prompt_id": "find-value"}).status_code == 200
    assert gated.post("/message", json={"text": "hi"}).status_code == 404
    assert gated.post("/conversations/x/message", json={"text": "hi"}).status_code == 404
    assert gated.get("/agents").status_code == 404
