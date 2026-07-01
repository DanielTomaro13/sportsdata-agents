"""Workbench B3 — per-agent model pins: prefs persistence, the /agents/model route,
and the harness seam (a pin wins over the caller's per-run tier, budgets unchanged)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from sportsdata_agents.agents import model_prefs
from sportsdata_agents.agents.harness import Harness
from sportsdata_agents.gateway.app import create_app
from sportsdata_agents.models.gateway import ModelReply
from sportsdata_agents.workspace import Workspace

pytestmark = pytest.mark.unit

WS = Workspace(tenant_id="t", workspace_id="w")


class FakeSession:
    agent_name = "orchestrator"
    recorder = None

    async def run(self, *a: Any, **k: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATA_DIR", str(tmp_path / "data"))


# ── prefs persistence ────────────────────────────────────────────────────


def test_prefs_roundtrip():
    assert model_prefs.load_overrides() == {}
    model_prefs.set_override("value_scout", "strong")
    model_prefs.set_override("news_scout", "anthropic/claude-x")
    assert model_prefs.load_overrides() == {"value_scout": "strong", "news_scout": "anthropic/claude-x"}
    assert model_prefs.override_for("value_scout") == "strong"
    model_prefs.set_override("value_scout", None)  # clear
    assert model_prefs.override_for("value_scout") is None
    assert model_prefs.load_overrides() == {"news_scout": "anthropic/claude-x"}


def test_hand_edited_garbage_is_ignored():
    # A hand-edited file with a non-tier, non-qualified value must not crash a run
    # at model-call time — override_for treats it as unset.
    model_prefs.set_override("value_scout", "strong")
    path = model_prefs._path()
    path.write_text('{"model_overrides": {"value_scout": "not a real tier"}}', encoding="utf-8")
    assert model_prefs.override_for("value_scout") is None


# ── gateway routes ───────────────────────────────────────────────────────


@pytest.fixture
async def client():
    app = create_app(session=FakeSession())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as c:
        yield c


async def test_agents_exposes_pin(client: httpx.AsyncClient):
    model_prefs.set_override("value_scout", "strong")
    r = await client.get("/agents")
    assert r.status_code == 200
    body = r.json()
    assert body["value_scout"]["tier_override"] == "strong"
    # unpinned agents report None, and the spec default is still there
    assert body["orchestrator"]["tier_override"] is None
    assert body["orchestrator"]["tier"]


async def test_pin_route_set_and_clear(client: httpx.AsyncClient):
    r = await client.post("/agents/model", json={"agent": "value_scout", "tier": "fast"})
    assert r.status_code == 200
    assert r.json() == {"agent": "value_scout", "tier_override": "fast"}
    assert model_prefs.override_for("value_scout") == "fast"

    r2 = await client.post("/agents/model", json={"agent": "value_scout", "tier": None})
    assert r2.json() == {"agent": "value_scout", "tier_override": None}
    assert model_prefs.override_for("value_scout") is None


async def test_pin_route_rejects_unknown_agent_and_bad_tier(client: httpx.AsyncClient):
    r = await client.post("/agents/model", json={"agent": "nope", "tier": "fast"})
    assert r.status_code == 404
    r2 = await client.post("/agents/model", json={"agent": "value_scout", "tier": "shiny"})
    assert r2.status_code == 400
    assert model_prefs.override_for("value_scout") is None  # nothing persisted


# ── harness seam ─────────────────────────────────────────────────────────


class TierRecordingProvider:
    """Answers immediately, recording the tier each call resolved to."""

    def __init__(self) -> None:
        self.tiers: list[str] = []

    async def complete(self, messages, *, tier="balanced", workspace, budget=None, **kw):  # type: ignore[no-untyped-def]
        self.tiers.append(tier)
        reply = ModelReply(text="done", model="fake", tokens_in=10, tokens_out=5, cost_usd=0.01)
        if budget is not None:
            budget.charge(reply.cost_usd)
        return reply


def _spec():
    from sportsdata_agents.agents.spec import AgentSpec

    return AgentSpec.model_validate(
        {"id": "test_agent", "display_name": "T", "system_prompt": "x", "model_tier": "balanced"}
    )


async def test_pin_wins_over_run_tier_and_spec_default():
    provider = TierRecordingProvider()
    h = Harness(_spec(), provider=provider, workspace=WS)

    # no pin: the caller's per-run tier stands, else the spec default
    await h.run("q", tier="fast")
    await h.run("q")
    assert provider.tiers == ["fast", "balanced"]

    # pinned: the pin wins over BOTH
    model_prefs.set_override("test_agent", "strong")
    await h.run("q", tier="fast")
    await h.run("q")
    assert provider.tiers[2:] == ["strong", "strong"]
