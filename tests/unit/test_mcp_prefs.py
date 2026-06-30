"""Workbench B1 — global MCP provider on/off: prefs persistence + the /mcp/toggle route.
(The actual tool-dropping enforcement is covered MCP-side by test_disabled_providers.)"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from sportsdata_agents.gateway.app import create_app
from sportsdata_agents.mcp import prefs

pytestmark = pytest.mark.unit


class FakeSession:
    agent_name = "orchestrator"
    recorder = None

    async def run(self, *a: Any, **k: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATA_DIR", str(tmp_path / "data"))


def test_prefs_roundtrip():
    assert prefs.load_disabled() == set()
    prefs.set_disabled("afl", disabled=True)
    prefs.set_disabled("nba", disabled=True)
    assert prefs.load_disabled() == {"afl", "nba"}
    assert prefs.disabled_env() == "afl,nba"  # sorted, comma-joined
    prefs.set_disabled("afl", disabled=False)
    assert prefs.load_disabled() == {"nba"}
    assert prefs.disabled_env() == "nba"


@pytest.fixture
async def client():
    app = create_app(session=FakeSession())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as c:
        yield c


async def test_toggle_route_persists(client: httpx.AsyncClient):
    r = await client.post("/mcp/toggle", json={"provider": "afl", "enabled": False})
    assert r.status_code == 200
    assert r.json() == {"provider": "afl", "enabled": False}
    assert prefs.load_disabled() == {"afl"}

    r2 = await client.post("/mcp/toggle", json={"provider": "afl", "enabled": True})
    assert r2.json()["enabled"] is True
    assert prefs.load_disabled() == set()


async def test_toggle_requires_provider(client: httpx.AsyncClient):
    r = await client.post("/mcp/toggle", json={"enabled": False})
    assert r.status_code == 400
