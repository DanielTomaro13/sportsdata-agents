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
    ops_ids = {"mcp_health", "repo_improver", "code_reviewer", "eval_benchmark",
               "incident_triage", "site_manager", "docs_keeper"}
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


async def test_site_status_reports_unreachable_not_crashes() -> None:
    """The site checker degrades to a structured error — never an exception
    (the agent escalates on ok=False; offline CI must not need the network)."""
    import os

    tools = {t.name: t for t in ops_tools()}
    os.environ["SPORTSDATA_AGENTS_SITE_URL"] = "http://127.0.0.1:9/"  # discard port: refused fast
    try:
        result = await tools["site_status"].execute({})
    finally:
        del os.environ["SPORTSDATA_AGENTS_SITE_URL"]
    assert result["ok"] is False and "error" in result


async def test_post_ops_report_needs_config() -> None:
    import os

    tools = {t.name: t for t in ops_tools()}
    saved = {k: os.environ.pop(k, None)
             for k in ("SLACK_BOT_TOKEN", "OPS_SLACK_CHANNEL", "OPS_DISCORD_WEBHOOK",
                       "OPS_NTFY_URL")}
    try:
        result = await tools["post_ops_report"].execute({"title": "t"})
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    assert result["pushed"] is False and "no operator target" in result["reason"]


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
    monkeypatch.delenv("OPS_DISCORD_WEBHOOK", raising=False)
    monkeypatch.delenv("OPS_NTFY_URL", raising=False)
    from sportsdata_agents.tools.ops import read_ops_state

    tools = {t.name: t for t in ops_tools()}
    out = await tools["escalate"].execute({"summary": "tab feed 401s", "details": "HTTP 401 x3"})
    assert out["escalated"] is True and out["pushed"] is False
    state = read_ops_state()
    assert state["escalations"][0]["summary"] == "tab feed 401s"


async def test_repo_file_access_rejects_prefix_sibling_escape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The repo confinement must be a real path containment check, not a string
    prefix: '/repo' must NOT grant access to '/repo-evil'."""
    import sportsdata_agents.tools.ops as ops_mod

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "ok.txt").write_text("inside", encoding="utf-8")
    evil = tmp_path / "repo-evil"
    evil.mkdir()
    (evil / "secret.txt").write_text("outside", encoding="utf-8")

    monkeypatch.setattr(ops_mod, "_repo_paths", lambda: {"repo": repo})
    tools = {t.name: t for t in ops_tools()}

    inside = await tools["read_repo_file"].execute({"repo": "repo", "path": "src/ok.txt"})
    assert inside["content"] == "inside"
    for path in ("../repo-evil/secret.txt", "../../repo-evil/secret.txt", "/etc/hosts"):
        with pytest.raises(ValueError, match="escapes the repo"):
            await tools["read_repo_file"].execute({"repo": "repo", "path": path})
