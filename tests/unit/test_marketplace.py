"""Workbench B6 — the marketplace route: plans + checkout links + feed-picker URL.
The app only HANDS OFF to the browser; there is no payment logic to test — just that
the storefront data is complete, well-formed, and env-overridable."""

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


async def test_marketplace_payload(client: httpx.AsyncClient):
    r = await client.get("/marketplace")
    assert r.status_code == 200
    body = r.json()
    skus = [p["sku"] for p in body["plans"]]
    assert skus == ["base", "sport_addon", "gambling_addon", "all_access"]
    for p in body["plans"]:
        assert p["url"].startswith("https://buy.stripe.com/")
        assert p["usd_month"] > 0 and p["name"] and p["desc"]
    assert body["feeds_url"].startswith("https://")
    # the account snapshot rides along so the pane shows the current plan
    assert "tier" in body["account"] and "addons" in body["account"]


async def test_marketplace_env_overrides(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setenv("SPORTSDATA_BUY_URL_BASE", "https://buy.stripe.com/test_override")
    monkeypatch.setenv("SPORTSDATA_FEEDS_URL", "https://example.test/feeds.html")
    r = await client.get("/marketplace")
    body = r.json()
    base = next(p for p in body["plans"] if p["sku"] == "base")
    assert base["url"] == "https://buy.stripe.com/test_override"
    assert body["feeds_url"] == "https://example.test/feeds.html"
    # the other links are untouched
    assert next(p for p in body["plans"] if p["sku"] == "all_access")["url"].endswith("cV203")
