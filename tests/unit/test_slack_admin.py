"""slack_manager's admin tools (fake client) + spec wiring."""

from __future__ import annotations

from typing import Any

import pytest

from sportsdata_agents.agents.loader import lint_specs, load_builtin_specs
from sportsdata_agents.tools.slack_admin import SLACK_ADMIN_TOOL_NAMES, slack_admin_tools

pytestmark = pytest.mark.unit


class FakeSlack:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def conversations_list(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("list", kw))
        return {
            "channels": [
                {"id": "C1", "name": "all-daniel", "topic": {"value": ""}, "is_member": True, "num_members": 2},
                {"id": "C2", "name": "nba-updates", "topic": {"value": "NBA"}, "is_member": False, "num_members": 1},
            ]
        }

    async def conversations_create(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("create", kw))
        return {"channel": {"id": "C9", "name": kw["name"]}}

    async def conversations_rename(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("rename", kw))
        return {"channel": {"id": kw["channel"], "name": kw["name"]}}

    async def conversations_archive(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("archive", kw))
        return {"ok": True}

    async def conversations_join(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("join", kw))
        return {"ok": True}

    async def conversations_setTopic(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("topic", kw))
        return {"ok": True}

    async def conversations_invite(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("invite", kw))
        return {"ok": True}

    async def conversations_members(self, **kw: Any) -> dict[str, Any]:
        return {"members": ["U1", "U2"]}

    async def chat_postMessage(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("post", kw))
        return {"ts": "1.2"}

    async def conversations_canvases_create(self, **kw: Any) -> dict[str, Any]:
        raise RuntimeError("not_allowed_token_type")  # free-plan behaviour


@pytest.fixture
def toolset() -> tuple[dict[str, Any], FakeSlack]:
    fake = FakeSlack()
    return {t.name: t for t in slack_admin_tools(fake)}, fake


async def test_audit_and_mutations(toolset: tuple[dict[str, Any], FakeSlack]) -> None:
    tools, fake = toolset
    chans = await tools["list_channels"].execute({})
    assert chans["channels"][0]["name"] == "all-daniel"

    created = await tools["create_channel"].execute({"name": "Alerts-CLV"})
    assert created["created"] == "alerts-clv"  # lowercased convention

    await tools["set_channel_topic"].execute({"channel_id": "C2", "topic": "NBA alerts land here"})
    assert ("join", {"channel": "C2"}) in fake.calls  # joins before setting

    renamed = await tools["rename_channel"].execute({"channel_id": "C2", "new_name": "NBA-Alerts"})
    assert renamed["renamed_to"] == "nba-alerts"

    await tools["invite_to_channel"].execute({"channel_id": "C1", "user_ids": ["U7", "U8"]})
    assert ("invite", {"channel": "C1", "users": "U7,U8"}) in fake.calls

    members = await tools["channel_members"].execute({"channel_id": "C1"})
    assert members["members"] == ["U1", "U2"]


async def test_canvas_degrades_clearly(toolset: tuple[dict[str, Any], FakeSlack]) -> None:
    tools, _ = toolset
    out = await tools["create_canvas"].execute({"channel_id": "C1", "markdown": "# Routing"})
    assert "unavailable" in out["error"] and "fallback" in out["error"]


def test_spec_grants_and_lints() -> None:
    specs = load_builtin_specs()
    spec = specs["slack_manager"]
    assert set(spec.tools.native) >= SLACK_ADMIN_TOOL_NAMES
    assert {"remember", "recall"} <= set(spec.tools.native)  # routing persistence
    assert spec.context.verify is False  # admin actions, not data claims
    assert lint_specs(specs) == []


async def test_unconfigured_session_degrades_to_stub() -> None:
    """Without SLACK_BOT_TOKEN the slack_manager still OPENS; tools answer with the
    actionable config error (same degradation contract as the DB tools)."""
    from sportsdata_agents.agents.runtime import AgentRuntime
    from sportsdata_agents.models.gateway import ModelReply, ToolCallRequest
    from sportsdata_agents.workspace import Workspace

    class P:
        calls = 0

        async def complete(self, messages, **kw):  # type: ignore[no-untyped-def]
            P.calls += 1
            if kw.get("budget"):
                kw["budget"].charge(0.001)
            if P.calls == 1:
                return ModelReply(text="", model="f", tokens_in=10, tokens_out=5, cost_usd=0.001,
                                  tool_calls=(ToolCallRequest(id="c", name="list_channels", arguments={}),))
            return ModelReply(text="cannot audit without config", model="f",
                              tokens_in=10, tokens_out=5, cost_usd=0.001)

    spec = load_builtin_specs()["slack_manager"]
    async with AgentRuntime(spec, provider=P(), workspace=Workspace(tenant_id="t", workspace_id="w")) as rt:
        res = await rt.run("audit the workspace")
    tool_msgs = [m for m in res.messages if m.get("role") == "tool"]
    assert any("SLACK_BOT_TOKEN" in (m.get("content") or "") for m in tool_msgs)
