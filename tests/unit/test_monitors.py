"""Workbench B5 — Monitors pane: the /alerts feed of fired arb/line_move/value alerts.
The route degrades to an empty list (not a 500) with no warehouse, mirroring /agents."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from sportsdata_agents.gateway.app import create_app

pytestmark = pytest.mark.unit


class FakeSession:
    agent_name = "orchestrator"
    recorder = None

    async def run(self, *a: Any, **k: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError


@pytest.fixture
async def client():
    app = create_app(session=FakeSession())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as c:
        yield c


async def test_alerts_graceful_empty(client: httpx.AsyncClient):
    r = await client.get("/alerts")
    assert r.status_code == 200
    body = r.json()
    assert body == {"alerts": []}  # no warehouse in the unit harness → empty, not a 500


async def test_alerts_kind_filter_accepted(client: httpx.AsyncClient):
    r = await client.get("/alerts", params={"kind": "arb", "limit": 10})
    assert r.status_code == 200
    assert "alerts" in r.json()
