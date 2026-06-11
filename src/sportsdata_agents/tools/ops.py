"""Operations-plane tools (M3.1, §3.1) — platform-credentialed, operator-only.

These tools hold the platform's blast radius: GitHub issues/PRs/reviews, local git
pushes, MCP health probes, feed remediation. They are NEVER wired into the customer
gateway — only the `agents ops` CLI injects them, and only into specs declaring
``plane: ops``. Structural guarantees baked in here:

- there is NO merge tool — a human merges every PR (§3.1 exit gate);
- ``propose_change`` refuses to commit to main and refuses paths outside the two
  platform repos;
- ``remediate_feed`` is a closed allow-list (retry / disable / enable) — anything
  else escalates to the operator instead of acting;
- the GitHub token is resolved lazily per call (env ``OPS_GITHUB_TOKEN``, falling
  back to the local git credential helper) and never returned in tool output.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.agents.harness import ToolDef

logger = logging.getLogger(__name__)

OPS_TOOL_NAMES = {
    "gh_create_issue", "gh_list_issues", "gh_list_prs", "gh_pr_diff",
    "gh_review_pr", "propose_change", "run_doctor", "run_contract_suite",
    "feed_health", "remediate_feed", "run_offline_evals", "record_agent_metrics",
    "escalate",
}

REMEDIATION_ALLOW_LIST = ("retry", "disable", "enable")
_GITHUB_API = "https://api.github.com"
_OUTPUT_CAP = 20_000  # chars of subprocess/diff output returned to the model


def ops_state_path() -> Path:
    root = Path(os.environ.get("SPORTSDATA_AGENTS_VAR_DIR", str(Path.home() / ".sportsdata-agents")))
    root.mkdir(parents=True, exist_ok=True)
    return root / "ops_state.json"


def read_ops_state() -> dict[str, Any]:
    path = ops_state_path()
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"disabled_feeds": []}


def write_ops_state(state: dict[str, Any]) -> None:
    state["updated_at"] = dt.datetime.now(dt.UTC).isoformat()
    ops_state_path().write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def disabled_feeds() -> set[str]:
    return set(read_ops_state().get("disabled_feeds") or [])


def _github_token() -> str:
    token = os.environ.get("OPS_GITHUB_TOKEN")
    if token:
        return token
    # local operator fallback: the git credential helper (never echoed)
    proc = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n",
        capture_output=True, text=True, timeout=30,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    raise RuntimeError("no GitHub credential: set OPS_GITHUB_TOKEN or configure a git credential helper")


def _repo_paths() -> dict[str, Path]:
    """The two platform repos this operator instance may touch. The agents repo is
    this package's checkout; the MCP repo is derived from the configured binary."""
    from sportsdata_agents.config import get_settings

    agents_repo = Path(__file__).resolve().parents[3]
    mcp_repo: Path | None = None
    command = get_settings().mcp_command
    if command:
        binary = Path(command[0])
        if ".venv" in binary.parts:
            mcp_repo = binary.parents[2]
    out = {"sportsdata-agents": agents_repo}
    if mcp_repo is not None and mcp_repo.is_dir():
        out["sportsdata-mcp"] = mcp_repo
    return out


def _origin_slug(repo_path: Path) -> str:
    """owner/name from the repo's origin remote — repos are config, not hardcoded."""
    url = subprocess.run(
        ["git", "-C", str(repo_path), "config", "--get", "remote.origin.url"],
        capture_output=True, text=True, timeout=10,
    ).stdout.strip()
    slug = url.removesuffix(".git")
    for prefix in ("https://github.com/", "git@github.com:"):
        if slug.startswith(prefix):
            return slug[len(prefix):]
    raise ValueError(f"origin of {repo_path} is not a GitHub remote: {url!r}")


async def _gh(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    import httpx

    headers = {"Authorization": f"token {_github_token()}",
               "Accept": "application/vnd.github+json"}
    async with httpx.AsyncClient(base_url=_GITHUB_API, headers=headers, timeout=30) as client:
        response = await client.request(method, path, json=payload)
    if response.status_code >= 400:
        raise RuntimeError(f"GitHub {method} {path} -> {response.status_code}: {response.text[:300]}")
    return response.json() if response.text else {}


def _run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 600) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True,
                          text=True, timeout=timeout)
    out = (proc.stdout + "\n" + proc.stderr).strip()
    return proc.returncode, out[-_OUTPUT_CAP:]


def ops_tools(session_factory: async_sessionmaker[AsyncSession] | None = None) -> list[ToolDef]:
    repos = _repo_paths()

    def _slug_for(repo: str) -> str:
        if repo not in repos:
            raise ValueError(f"unknown repo {repo!r}; operator repos: {sorted(repos)}")
        return _origin_slug(repos[repo])

    async def gh_create_issue(args: dict[str, Any]) -> Any:
        """{repo: sportsdata-agents|sportsdata-mcp, title, body, labels?} → open a
        GitHub issue (the QA agent's 'file issues on real breaks')."""
        slug = _slug_for(str(args["repo"]))
        issue = await _gh("POST", f"/repos/{slug}/issues", {
            "title": str(args["title"]), "body": str(args.get("body", "")),
            "labels": list(args.get("labels") or []),
        })
        return {"number": issue.get("number"), "url": issue.get("html_url")}

    async def gh_list_issues(args: dict[str, Any]) -> Any:
        """{repo, state?: open|closed|all} → recent issues (avoid duplicate filings)."""
        slug = _slug_for(str(args["repo"]))
        rows = await _gh("GET", f"/repos/{slug}/issues?state={args.get('state', 'open')}&per_page=30")
        return {"issues": [
            {"number": r["number"], "title": r["title"], "state": r["state"],
             "is_pr": "pull_request" in r}
            for r in rows
        ]}

    async def gh_list_prs(args: dict[str, Any]) -> Any:
        """{repo, state?: open|closed|all} → recent pull requests."""
        slug = _slug_for(str(args["repo"]))
        rows = await _gh("GET", f"/repos/{slug}/pulls?state={args.get('state', 'open')}&per_page=30")
        return {"prs": [
            {"number": r["number"], "title": r["title"], "state": r["state"],
             "head": r["head"]["ref"], "url": r["html_url"]}
            for r in rows
        ]}

    async def gh_pr_diff(args: dict[str, Any]) -> Any:
        """{repo, number} → the PR's unified diff (capped) for review."""
        import httpx

        slug = _slug_for(str(args["repo"]))
        headers = {"Authorization": f"token {_github_token()}",
                   "Accept": "application/vnd.github.diff"}
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{_GITHUB_API}/repos/{slug}/pulls/{int(args['number'])}",
                                        headers=headers)
        if response.status_code >= 400:
            raise RuntimeError(f"GitHub diff -> {response.status_code}")
        return {"diff": response.text[:_OUTPUT_CAP], "truncated": len(response.text) > _OUTPUT_CAP}

    async def gh_review_pr(args: dict[str, Any]) -> Any:
        """{repo, number, verdict: approve|request_changes|comment, body} → submit a
        PR review. There is deliberately NO merge tool: a human merges (§3.1)."""
        verdict = str(args["verdict"]).lower()
        events = {"approve": "APPROVE", "request_changes": "REQUEST_CHANGES", "comment": "COMMENT"}
        if verdict not in events:
            raise ValueError(f"verdict must be one of {sorted(events)}")
        slug = _slug_for(str(args["repo"]))
        review = await _gh("POST", f"/repos/{slug}/pulls/{int(args['number'])}/reviews",
                           {"event": events[verdict], "body": str(args.get("body", ""))})
        return {"review_id": review.get("id"), "state": review.get("state")}

    async def propose_change(args: dict[str, Any]) -> Any:
        """{repo, branch, files: [{path, content}], commit_message, pr_title, pr_body}
        → write files on a NEW branch, commit, push, open a PR. Refuses main; refuses
        paths escaping the repo; the PR is the only output — a human merges."""
        repo = str(args["repo"])
        branch = str(args["branch"]).strip()
        if repo not in repos:
            raise ValueError(f"unknown repo {repo!r}; operator repos: {sorted(repos)}")
        if branch in ("main", "master") or not branch:
            raise ValueError("refused: changes go through a PR branch, never main (§3.1)")
        repo_path = repos[repo]
        files = list(args.get("files") or [])
        if not files:
            raise ValueError("files must be a non-empty list of {path, content}")
        rc, out = _run(["git", "-C", str(repo_path), "status", "--porcelain"])
        if rc != 0:
            raise RuntimeError(f"git status failed: {out}")
        if out.strip():
            raise RuntimeError("refused: the working tree has uncommitted changes — operator must resolve first")
        for step in (
            ["git", "-C", str(repo_path), "fetch", "origin", "main"],
            ["git", "-C", str(repo_path), "checkout", "-B", branch, "origin/main"],
        ):
            rc, out = _run(step)
            if rc != 0:
                raise RuntimeError(f"{' '.join(step[3:])} failed: {out}")
        try:
            for f in files:
                target = (repo_path / str(f["path"])).resolve()
                if not str(target).startswith(str(repo_path.resolve())):
                    raise ValueError(f"refused: {f['path']!r} escapes the repo")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(f["content"]), encoding="utf-8")
            message = str(args["commit_message"]).rstrip() + (
                "\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
            )
            for step in (
                ["git", "-C", str(repo_path), "add", *[str(f["path"]) for f in files]],
                ["git", "-C", str(repo_path), "commit", "-m", message],
                ["git", "-C", str(repo_path), "push", "-u", "origin", branch],
            ):
                rc, out = _run(step)
                if rc != 0:
                    raise RuntimeError(f"{step[3]} failed: {out}")
            slug = _origin_slug(repo_path)
            pr = await _gh("POST", f"/repos/{slug}/pulls", {
                "title": str(args["pr_title"]), "body": str(args.get("pr_body", "")),
                "head": branch, "base": "main",
            })
            return {"pr": pr.get("number"), "url": pr.get("html_url"), "branch": branch}
        finally:  # the operator's checkout goes back to main regardless
            _run(["git", "-C", str(repo_path), "checkout", "main"])

    async def run_doctor(args: dict[str, Any]) -> Any:
        """Run the data plane's `doctor` (provider config + connectivity probes)."""
        from sportsdata_agents.config import get_settings

        command = get_settings().mcp_command
        rc, out = await asyncio.to_thread(_run, [command[0], "doctor"], timeout=600)
        return {"ok": rc == 0, "output": out}

    async def run_contract_suite(args: dict[str, Any]) -> Any:
        """Run the MCP repo's live contract tests (response structure vs the docs)."""
        mcp_repo = repos.get("sportsdata-mcp")
        if mcp_repo is None:
            raise RuntimeError("MCP repo not found next to the configured binary")
        pytest_bin = mcp_repo / ".venv" / "bin" / "pytest"
        rc, out = await asyncio.to_thread(
            _run, [str(pytest_bin), "tests", "-m", "contract", "-q"], cwd=mcp_repo, timeout=1800
        )
        return {"ok": rc == 0, "output": out[-6000:]}

    async def feed_health(args: dict[str, Any]) -> Any:
        """{hours?: 6} → per-provider snapshot counts + freshness from the warehouse —
        AGGREGATED signals only (§3.1); a provider silent for 3x its cadence is stale."""
        if session_factory is None:
            raise RuntimeError("feed_health needs the warehouse database configured")
        from sportsdata_agents.data.models import OddsSnapshot
        from sportsdata_agents.operations.ingestion import FEEDS

        hours = float(args.get("hours", 6))
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(OddsSnapshot.provider, func.count(), func.max(OddsSnapshot.captured_at))
                    .where(OddsSnapshot.captured_at >= cutoff)
                    .group_by(OddsSnapshot.provider)
                )
            ).all()
        seen = {provider: {"snapshots": n, "latest": str(latest)} for provider, n, latest in rows}
        stale = []
        now = dt.datetime.now(dt.UTC)
        for feed in FEEDS.values():
            provider_rows = [v for k, v in seen.items() if k.startswith(feed.name.split("_")[0])]
            grace = dt.timedelta(seconds=feed.interval_s * 3)
            if feed.name in disabled_feeds():
                continue
            if not provider_rows:
                stale.append({"feed": feed.name, "reason": f"no snapshots in {hours}h"})
                continue
            latest = max(dt.datetime.fromisoformat(r["latest"]) for r in provider_rows)
            if latest.tzinfo is None:
                latest = latest.replace(tzinfo=dt.UTC)
            if now - latest > grace:
                stale.append({"feed": feed.name, "reason": f"silent since {latest.isoformat()}"})
        return {"providers": seen, "stale_feeds": stale, "disabled_feeds": sorted(disabled_feeds())}

    async def remediate_feed(args: dict[str, Any]) -> Any:
        """{feed, action: retry|disable|enable} — the CLOSED remediation allow-list
        (§3.1). retry runs the feed once now; disable/enable flip durable ops state
        (the ingest CLI skips disabled feeds). Anything beyond this: escalate."""
        from sportsdata_agents.operations.ingestion import FEEDS

        feed = str(args["feed"])
        action = str(args["action"]).lower()
        if action not in REMEDIATION_ALLOW_LIST:
            raise ValueError(
                f"refused: {action!r} is outside the remediation allow-list "
                f"{REMEDIATION_ALLOW_LIST} — use escalate instead"
            )
        if feed not in FEEDS:
            raise ValueError(f"unknown feed {feed!r}")
        state = read_ops_state()
        disabled = set(state.get("disabled_feeds") or [])
        if action == "disable":
            disabled.add(feed)
        elif action == "enable":
            disabled.discard(feed)
        state["disabled_feeds"] = sorted(disabled)
        write_ops_state(state)
        if action == "retry":
            if session_factory is None:
                raise RuntimeError("retry needs the warehouse database configured")
            from sportsdata_agents.config import get_settings
            from sportsdata_agents.mcp.manager import MCPManager
            from sportsdata_agents.operations.ingestion import ingest_once
            from sportsdata_agents.operations.ingestion.worker import INGEST_MAX_BYTES

            target = FEEDS[feed]
            async with MCPManager(
                groups=list(target.mcp_groups),
                command=get_settings().mcp_command,
                extra_env={"SPORTSDATA_MCP_MAX_BYTES": str(INGEST_MAX_BYTES)},
            ) as manager:
                report = await ingest_once(manager, session_factory, [target])
            return {"action": "retry", "feed": feed, "report": report.get(feed)}
        return {"action": action, "feed": feed, "disabled_feeds": state["disabled_feeds"]}

    async def run_offline_evals(args: dict[str, Any]) -> Any:
        """Run the offline eval suite and gate it against the committed baseline."""
        from sportsdata_agents.evals.runner import (
            gate_against_baseline,
            load_baseline,
        )
        from sportsdata_agents.evals.runner import (
            run_offline_evals as _run_evals,
        )

        scores = await _run_evals()
        problems = gate_against_baseline(scores, load_baseline())
        return {"scores": {s.name: s.score for s in scores},
                "details": {s.name: s.details for s in scores},
                "regressions": problems, "ok": not problems}

    async def record_agent_metrics(args: dict[str, Any]) -> Any:
        """{agent, runs, success_rate?, cost_per_success_usd?, avg_latency_ms?, quality?}
        → append an agent_metrics window row (the §16 rollup the eval agent owns)."""
        if session_factory is None:
            raise RuntimeError("record_agent_metrics needs the database configured")
        from decimal import Decimal

        from sportsdata_agents.data.models import AgentMetric

        now = dt.datetime.now(dt.UTC)
        async with session_factory() as session:
            session.add(AgentMetric(
                tenant_id="platform", workspace_id="ops",
                agent=str(args["agent"]),
                window_start=now - dt.timedelta(days=7), window_end=now,
                runs=int(args.get("runs", 0)),
                success_rate=Decimal(str(args["success_rate"])) if args.get("success_rate") is not None else None,
                cost_per_success_usd=(
                    Decimal(str(args["cost_per_success_usd"]))
                    if args.get("cost_per_success_usd") is not None else None
                ),
                avg_latency_ms=int(args["avg_latency_ms"]) if args.get("avg_latency_ms") is not None else None,
                quality=args.get("quality") or {},
            ))
            await session.commit()
        return {"recorded": str(args["agent"])}

    async def escalate(args: dict[str, Any]) -> Any:
        """{summary, details?} → report to the operator: durable ops-state entry +
        Slack push when configured. The escape hatch for anything outside the
        remediation allow-list."""
        summary = str(args["summary"])
        state = read_ops_state()
        state.setdefault("escalations", []).append({
            "at": dt.datetime.now(dt.UTC).isoformat(),
            "summary": summary,
            "details": str(args.get("details", ""))[:2000],
        })
        write_ops_state(state)
        pushed = False
        token = os.environ.get("SLACK_BOT_TOKEN")
        channel = os.environ.get("OPS_SLACK_CHANNEL")
        if token and channel:
            import httpx

            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    "https://slack.com/api/chat.postMessage",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"channel": channel, "text": f":rotating_light: ops escalation: {summary}"},
                )
            pushed = bool(response.json().get("ok"))
        logger.warning("ops escalation: %s", summary)
        return {"escalated": True, "slack_pushed": pushed,
                "state_file": str(ops_state_path())}

    def _tool(name: str, fn: Any, props: dict[str, Any], required: list[str]) -> ToolDef:
        return ToolDef(
            name=name,
            description=(fn.__doc__ or name).strip().splitlines()[0],
            parameters={"type": "object", "properties": props, "required": required},
            execute=fn,
        )

    return [
        _tool("gh_create_issue", gh_create_issue,
              {"repo": {"type": "string"}, "title": {"type": "string"},
               "body": {"type": "string"}, "labels": {"type": "array", "items": {"type": "string"}}},
              ["repo", "title"]),
        _tool("gh_list_issues", gh_list_issues,
              {"repo": {"type": "string"}, "state": {"type": "string"}}, ["repo"]),
        _tool("gh_list_prs", gh_list_prs,
              {"repo": {"type": "string"}, "state": {"type": "string"}}, ["repo"]),
        _tool("gh_pr_diff", gh_pr_diff,
              {"repo": {"type": "string"}, "number": {"type": "integer"}}, ["repo", "number"]),
        _tool("gh_review_pr", gh_review_pr,
              {"repo": {"type": "string"}, "number": {"type": "integer"},
               "verdict": {"type": "string", "enum": ["approve", "request_changes", "comment"]},
               "body": {"type": "string"}},
              ["repo", "number", "verdict"]),
        _tool("propose_change", propose_change,
              {"repo": {"type": "string"}, "branch": {"type": "string"},
               "files": {"type": "array", "items": {
                   "type": "object",
                   "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                   "required": ["path", "content"]}},
               "commit_message": {"type": "string"}, "pr_title": {"type": "string"},
               "pr_body": {"type": "string"}},
              ["repo", "branch", "files", "commit_message", "pr_title"]),
        _tool("run_doctor", run_doctor, {}, []),
        _tool("run_contract_suite", run_contract_suite, {}, []),
        _tool("feed_health", feed_health, {"hours": {"type": "number"}}, []),
        _tool("remediate_feed", remediate_feed,
              {"feed": {"type": "string"},
               "action": {"type": "string", "enum": list(REMEDIATION_ALLOW_LIST)}},
              ["feed", "action"]),
        _tool("run_offline_evals", run_offline_evals, {}, []),
        _tool("record_agent_metrics", record_agent_metrics,
              {"agent": {"type": "string"}, "runs": {"type": "integer"},
               "success_rate": {"type": "number"}, "cost_per_success_usd": {"type": "number"},
               "avg_latency_ms": {"type": "integer"}, "quality": {"type": "object"}},
              ["agent", "runs"]),
        _tool("escalate", escalate,
              {"summary": {"type": "string"}, "details": {"type": "string"}}, ["summary"]),
    ]
