"""M3.1 — the operations plane's structural guarantees (§3.1).

The split is enforced in infrastructure, not prompts: the gateway can't open ops
agents, product agents can't delegate to them, the remediation allow-list is
closed, there is no merge tool, and propose_change can't touch main.
"""

from __future__ import annotations

from typing import Any

import pytest

from sportsdata_agents.agents.loader import lint_specs, load_builtin_specs, load_spec_text
from sportsdata_agents.tools.ops import OPS_TOOL_NAMES, REMEDIATION_ALLOW_LIST, ops_tools

pytestmark = pytest.mark.unit


def _spec(id_: str, plane: str, delegate: list[str] | None = None) -> Any:
    return load_spec_text(f"""
spec_version: 1
agent:
  id: {id_}
  display_name: X
  plane: {plane}
  system_prompt: x
  can_delegate_to: [{', '.join(delegate or [])}]
""")


def test_ops_agents_exist_and_are_ops_plane() -> None:
    specs = load_builtin_specs()
    ops_ids = {"mcp_health", "repo_improver", "code_reviewer", "eval_benchmark", "incident_triage"}
    assert ops_ids <= set(specs)
    assert all(specs[i].plane == "ops" for i in ops_ids)
    # every other (product) agent stays product-plane
    assert all(s.plane == "product" for i, s in specs.items() if i not in ops_ids)


def test_product_agents_cannot_delegate_to_ops() -> None:
    specs = {
        "orchestrator": _spec("orchestrator", "product", ["repo_improver"]),
        "repo_improver": _spec("repo_improver", "ops"),
    }
    problems = lint_specs(specs)
    assert any("cannot delegate to ops-plane" in p for p in problems)


def test_gateway_refuses_ops_agents() -> None:
    from sportsdata_agents.gateway.service import TeamSession

    with pytest.raises(PermissionError, match="ops-plane"):
        TeamSession(agent_id="repo_improver")
    # team mode silently excludes them — no path from customer traffic (§3.1)
    session = TeamSession()
    assert all(s.plane != "ops" for s in session.specs.values())
    # the operator CLI's flag is the only way in
    assert TeamSession(agent_id="repo_improver", allow_ops=True).agent_id == "repo_improver"


def test_there_is_no_merge_tool() -> None:
    names = {t.name for t in ops_tools()}
    assert names == OPS_TOOL_NAMES
    assert not any("merge" in n for n in names)  # a human merges — structurally


async def test_remediation_allow_list_is_closed() -> None:
    tools = {t.name: t for t in ops_tools()}
    assert REMEDIATION_ALLOW_LIST == ("retry", "disable", "enable")
    with pytest.raises(ValueError, match="allow-list"):
        await tools["remediate_feed"].execute({"feed": "tab_racing", "action": "delete"})
    with pytest.raises(ValueError, match="allow-list"):
        await tools["remediate_feed"].execute({"feed": "tab_racing", "action": "rewrite_config"})
    with pytest.raises(ValueError, match="unknown feed"):
        await tools["remediate_feed"].execute({"feed": "nope", "action": "disable"})


async def test_disable_enable_round_trip(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPORTSDATA_AGENTS_VAR_DIR", str(tmp_path))
    from sportsdata_agents.tools.ops import disabled_feeds

    tools = {t.name: t for t in ops_tools()}
    out = await tools["remediate_feed"].execute({"feed": "tab_racing", "action": "disable"})
    assert out["disabled_feeds"] == ["tab_racing"]
    assert disabled_feeds() == {"tab_racing"}
    out = await tools["remediate_feed"].execute({"feed": "tab_racing", "action": "enable"})
    assert out["disabled_feeds"] == []


async def test_propose_change_refuses_main_and_escapes(tmp_path: Any) -> None:
    tools = {t.name: t for t in ops_tools()}
    base = {"files": [{"path": "x.txt", "content": "x"}],
            "commit_message": "m", "pr_title": "t"}
    with pytest.raises(ValueError, match="never main"):
        await tools["propose_change"].execute({"repo": "sportsdata-agents", "branch": "main", **base})
    with pytest.raises(ValueError, match="never main"):
        await tools["propose_change"].execute({"repo": "sportsdata-agents", "branch": "", **base})
    with pytest.raises(ValueError, match="unknown repo"):
        await tools["propose_change"].execute({"repo": "someones-laptop", "branch": "ops/x", **base})


async def test_escalate_is_durable(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPORTSDATA_AGENTS_VAR_DIR", str(tmp_path))
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    from sportsdata_agents.tools.ops import read_ops_state

    tools = {t.name: t for t in ops_tools()}
    out = await tools["escalate"].execute({"summary": "tab feed 401s", "details": "HTTP 401 x3"})
    assert out["escalated"] is True and out["slack_pushed"] is False
    state = read_ops_state()
    assert state["escalations"][0]["summary"] == "tab feed 401s"
