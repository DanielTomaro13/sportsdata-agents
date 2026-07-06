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
import contextlib
import datetime as dt
import json
import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.agents.harness import ToolDef

logger = logging.getLogger(__name__)

OPS_TOOL_NAMES = {
    "gh_create_issue", "gh_list_issues", "gh_list_prs", "gh_pr_diff",
    "gh_review_pr", "list_repo_files", "read_repo_file", "propose_change",
    "run_doctor", "run_contract_suite", "feed_health", "remediate_feed",
    "run_offline_evals", "record_agent_metrics", "delegation_stats", "alert_quality",
    "escalate",
    "site_status", "site_audit", "site_traffic", "post_ops_report",
}

# The public marketing site (GitHub Pages, playback mode) and the PUBLIC repo
# that hosts it — the site_manager agent's beat. Env-overridable for tests/moves.
_SITE_URL_ENV = "SPORTSDATA_AGENTS_SITE_URL"
_SITE_REPO_ENV = "SPORTSDATA_AGENTS_SITE_REPO"
_SITE_URL_DEFAULT = "https://danieltomaro13.github.io/sportsdata-site/"
_SITE_REPO_DEFAULT = "DanielTomaro13/sportsdata-site"


def site_url() -> str:
    return os.environ.get(_SITE_URL_ENV, _SITE_URL_DEFAULT)

REMEDIATION_ALLOW_LIST = ("retry", "disable", "enable")
_GITHUB_API = "https://api.github.com"
_OUTPUT_CAP = 20_000  # chars of subprocess/diff output returned to the model


def ops_state_path() -> Path:
    from sportsdata_agents.paths import ops_dir

    return ops_dir() / "ops_state.json"


_OPS_STATE_THREAD_LOCK = threading.Lock()


def read_ops_state() -> dict[str, Any]:
    path = ops_state_path()
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # a torn read (another writer mid-truncate) — treat as empty rather
            # than crash the caller; the atomic write below makes this rare
            return {"disabled_feeds": []}
    return {"disabled_feeds": []}


def write_ops_state(state: dict[str, Any]) -> None:
    """Atomic write: parallel scheduler jobs (in threads) and the custodian
    subprocess all read-modify-write this file — a plain truncate-then-write let
    a reader see a half-written file and let simultaneous writers lose each
    other's updates. tempfile + os.replace is atomic on POSIX."""
    import tempfile

    state["updated_at"] = dt.datetime.now(dt.UTC).isoformat()
    path = ops_state_path()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(state, indent=2) + "\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def ops_state_locked() -> Any:
    """Exclusive cross-process + cross-thread lock for a read-modify-write of ops
    state (the scheduler's record_outcome and the custodian both mutate it).
    Serialises so concurrent increments never lose each other."""
    with _OPS_STATE_THREAD_LOCK, contextlib.ExitStack() as stack:
        try:
            import fcntl

            fh = stack.enter_context(open(ops_state_path().with_suffix(".lock"), "w"))
            fcntl.flock(fh, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield


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

    async def list_repo_files(args: dict[str, Any]) -> Any:
        """{repo, pattern?: glob} → tracked files (git ls-files) so the improver can
        find what to change without shelling out."""
        repo = str(args["repo"])
        if repo not in repos:
            raise ValueError(f"unknown repo {repo!r}; operator repos: {sorted(repos)}")
        rc, out = _run(["git", "-C", str(repos[repo]), "ls-files", str(args.get("pattern", ""))])
        if rc != 0:
            raise RuntimeError(out)
        files = [line for line in out.splitlines() if line]
        return {"files": files[:400], "truncated": len(files) > 400}

    async def read_repo_file(args: dict[str, Any]) -> Any:
        """{repo, path} → the file's current content (read-only, repo-confined,
        capped) — what propose_change edits must be based on."""
        repo = str(args["repo"])
        if repo not in repos:
            raise ValueError(f"unknown repo {repo!r}; operator repos: {sorted(repos)}")
        target = (repos[repo] / str(args["path"])).resolve()
        # is_relative_to, not a string prefix: "/repo" must not match "/repo-evil"
        if not target.is_relative_to(repos[repo].resolve()):
            raise ValueError(f"refused: {args['path']!r} escapes the repo")
        if not target.is_file():
            raise FileNotFoundError(str(args["path"]))
        text = target.read_text(encoding="utf-8")
        return {"content": text[:_OUTPUT_CAP * 4], "truncated": len(text) > _OUTPUT_CAP * 4}

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
        """{repo, branch, files: [{path, content} | {path, find, replace}],
        commit_message, pr_title, pr_body} → apply edits on a NEW branch, commit,
        push, open a PR. find/replace is the surgical form (find must match the
        file EXACTLY ONCE); content overwrites whole (new) files. Refuses main;
        refuses paths escaping the repo; the PR is the only output — a human merges."""
        repo = str(args["repo"])
        branch = str(args["branch"]).strip()
        if repo not in repos:
            raise ValueError(f"unknown repo {repo!r}; operator repos: {sorted(repos)}")
        if branch in ("main", "master") or not branch:
            raise ValueError("refused: changes go through a PR branch, never main (§3.1)")
        repo_path = repos[repo]
        files = list(args.get("files") or [])
        if not files:
            raise ValueError("files must be a non-empty list of {path, content} or {path, find, replace}")
        rc, out = _run(["git", "-C", str(repo_path), "status", "--porcelain"])
        if rc != 0:
            raise RuntimeError(f"git status failed: {out}")
        if out.strip():
            raise RuntimeError("refused: the working tree has uncommitted changes — operator must resolve first")
        rc, original_branch = _run(["git", "-C", str(repo_path), "rev-parse", "--abbrev-ref", "HEAD"])
        original_branch = original_branch.strip() or "main"
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
                # is_relative_to, not a string prefix: "/repo" must not match "/repo-evil"
                if not target.is_relative_to(repo_path.resolve()):
                    raise ValueError(f"refused: {f['path']!r} escapes the repo")
                if "find" in f:  # surgical edit — never reproduce the whole file
                    text = target.read_text(encoding="utf-8")
                    hits = text.count(str(f["find"]))
                    if hits != 1:
                        raise ValueError(
                            f"{f['path']}: find matched {hits} times — it must match exactly once"
                        )
                    target.write_text(
                        text.replace(str(f["find"]), str(f.get("replace", "")), 1),
                        encoding="utf-8",
                    )
                else:
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
        finally:  # the operator's checkout goes back to WHERE IT WAS (audit fix)
            _run(["git", "-C", str(repo_path), "checkout", original_branch])

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
            # EXACT provider match — prefix matching let a dead tab_racing hide
            # behind fresh tab sports captures (audit finding)
            row = seen.get(feed.provider)
            grace = dt.timedelta(seconds=feed.interval_s * 3)
            if feed.name in disabled_feeds():
                continue
            if row is None:
                stale.append({"feed": feed.name, "provider": feed.provider,
                              "reason": f"no snapshots in {hours}h"})
                continue
            latest = dt.datetime.fromisoformat(row["latest"])
            if latest.tzinfo is None:
                latest = latest.replace(tzinfo=dt.UTC)
            if now - latest > grace:
                stale.append({"feed": feed.name, "provider": feed.provider,
                              "reason": f"silent since {latest.isoformat()}"})
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

    async def alert_quality(args: dict[str, Any]) -> Any:
        """{days?: 7} → are the watches firing on TAKEABLE opportunities?
        Per kind: alerts fired; for arbs: how many were re-measured 5min later,
        how many were still live, and the median margin decay. Aggregates only."""
        if session_factory is None:
            raise RuntimeError("alert_quality needs the database configured")
        import statistics

        from sportsdata_agents.data.models import Alert

        days = float(args.get("days", 7))
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
        async with session_factory() as session:
            rows = (
                await session.execute(select(Alert).where(Alert.created_at >= cutoff))
            ).scalars().all()
        by_kind: dict[str, int] = {}
        stats: dict[str, dict[str, Any]] = {
            "arb": {"measured": 0, "still": 0, "decays": []},
            "value": {"measured": 0, "still": 0, "decays": []},
        }
        for alert in rows:
            by_kind[alert.kind] = by_kind.get(alert.kind, 0) + 1
            bucket = stats.get(alert.kind)
            if bucket is None:
                continue
            payload = alert.payload or {}
            outcome = payload.get("outcome")
            if not outcome:
                continue
            bucket["measured"] += 1
            if outcome.get("still_arb") or outcome.get("still_value"):
                bucket["still"] += 1
            after = outcome.get("margin_pct_after", outcome.get("edge_pct_after"))
            before = payload.get("margin_pct", payload.get("edge_pct"))
            if after is not None and before is not None:
                bucket["decays"].append(round(float(before) - float(after), 2))

        def _summary(bucket: dict[str, Any]) -> dict[str, Any]:
            measured, still, decays = bucket["measured"], bucket["still"], bucket["decays"]
            return {
                "measured": measured,
                "still_live_after_5m": still,
                "takeable_rate": round(still / measured, 3) if measured else None,
                "median_decay_pct": round(statistics.median(decays), 2) if decays else None,
            }

        return {
            "days": days,
            "fired_by_kind": by_kind,
            "arb": _summary(stats["arb"]),
            "value": _summary(stats["value"]),
            "note": "decay = edge/margin at fire minus 5min later; a high takeable "
                    "rate means alerts arrive while the window is still open",
        }

    async def delegation_stats(args: dict[str, Any]) -> Any:
        """{days?: 7} → how the orchestrator's complexity routing behaved: runs,
        cost and avg latency per (agent, tier) — is it over- or under-escalating?
        Aggregates only (§3.1)."""
        if session_factory is None:
            raise RuntimeError("delegation_stats needs the database configured")
        from sportsdata_agents.data.models import AgentRun

        days = float(args.get("days", 7))
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(AgentRun.agent, AgentRun.tier,
                           func.count(), func.sum(AgentRun.cost_usd),
                           func.avg(AgentRun.latency_ms))
                    .where(AgentRun.created_at >= cutoff)
                    .group_by(AgentRun.agent, AgentRun.tier)
                )
            ).all()
        out = [
            {"agent": agent, "tier": tier or "?", "runs": n,
             "cost_usd": round(float(cost or 0), 4),
             "avg_latency_ms": int(latency or 0)}
            for agent, tier, n, cost, latency in rows
        ]
        out.sort(key=lambda r: -r["cost_usd"])
        return {"days": days, "by_agent_tier": out,
                "total_cost_usd": round(sum(r["cost_usd"] for r in out), 4)}

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

    async def site_status(args: dict[str, Any]) -> Any:
        """{} → is the public site up and intact: HTTP status, latency, and the
        structural markers (playback flag, marquee band, demo fallback)."""
        import time as _time

        import httpx

        url = site_url()
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                t0 = _time.monotonic()
                page = await client.get(url)
                latency_ms = int((_time.monotonic() - t0) * 1000)
                fallback = await client.get(url.rstrip("/") + "/demo-fallback.json")
        except httpx.HTTPError as e:
            return {"ok": False, "url": url, "error": f"{type(e).__name__}: {e}"}
        html = page.text
        return {
            "ok": page.status_code == 200 and fallback.status_code == 200,
            "url": url,
            "status_code": page.status_code,
            "latency_ms": latency_ms,
            "bytes": len(page.content),
            "playback_mode": "window.GATEWAY_URL = null" in html,
            "has_marquees": 'id="row-sports"' in html,
            "fallback_ok": fallback.status_code == 200,
        }

    async def site_audit(args: dict[str, Any]) -> Any:
        """{} → drift between the LIVE site and the data plane: published counters
        vs the real catalogue, and providers whose name appears nowhere on the
        page. Display names legitimately differ (entain → Ladbrokes/Neds;
        fanduel_racing → FanDuel Racing) — judge, don't churn."""
        import json as _json

        import httpx

        from sportsdata_agents.config import get_settings
        from sportsdata_agents.mcp.manager import MCPManager

        url = site_url().rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                page = await client.get(url + "/")
                fallback = await client.get(url + "/demo-fallback.json")
            live_stats = (_json.loads(fallback.text) or {}).get("stats", {})
            html = page.text.lower()
        except (httpx.HTTPError, ValueError) as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        async with MCPManager(groups=["*"], command=get_settings().mcp_command) as manager:
            payload = await manager.call_tool("list_available_groups", {})
        available = payload.get("available") or {}
        providers = sorted({str(info.get("provider", group.split(".")[0]))
                            for group, info in available.items()})
        catalogue = {
            "providers": len(providers),
            "groups": len(available),
            "tools": sum(int(info.get("tools", 0)) for info in available.values()),
        }
        missing = [p for p in providers if p.replace("_", " ").lower() not in html]
        return {
            "ok": True,
            "live_stats": live_stats,
            "catalogue": catalogue,
            "counts_drift": any(live_stats.get(k) != v for k, v in catalogue.items()),
            "providers_not_on_page": missing,
            "note": "display names differ legitimately for some providers — "
                    "verify before proposing a change",
        }

    async def site_traffic(args: dict[str, Any]) -> Any:
        """{days?: 14} → traffic for the PUBLIC site repo from the GitHub traffic
        API (views/clones are the repo's, the closest signal Pages exposes) plus
        referrers, popular paths, and the lead count from the gateway DB."""
        import httpx

        repo = os.environ.get(_SITE_REPO_ENV, _SITE_REPO_DEFAULT)
        token = _github_token()
        headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
        out: dict[str, Any] = {"repo": repo, "site": site_url()}
        async with httpx.AsyncClient(timeout=20) as client:
            for key, path in (("views", "traffic/views"), ("clones", "traffic/clones"),
                              ("referrers", "traffic/popular/referrers"),
                              ("paths", "traffic/popular/paths")):
                response = await client.get(f"{_GITHUB_API}/repos/{repo}/{path}", headers=headers)
                out[key] = response.json() if response.status_code == 200 else {
                    "error": response.status_code}
        if session_factory is not None:
            try:
                from sportsdata_agents.data.models import Lead

                async with session_factory() as session:
                    out["leads_total"] = int(
                        (await session.execute(select(func.count()).select_from(Lead))).scalar() or 0
                    )
            except Exception as e:  # the leads table is optional signal, never a failure
                out["leads_total"] = None
                out["leads_note"] = f"{type(e).__name__}: {e}"
        out["note"] = ("GitHub traffic counts the REPO, not Pages page views — "
                       "page-level analytics needs a privacy-friendly snippet (P4 call)")
        return out

    async def post_ops_report(args: dict[str, Any]) -> Any:
        """{title, body} → push an operator report to every configured target
        (Slack OPS_SLACK_CHANNEL, Discord OPS_DISCORD_WEBHOOK). For routine
        summaries — escalate() is for incidents."""
        from sportsdata_agents.observability.notify import operator_broadcast

        title = str(args["title"])
        body = str(args.get("body", ""))[:3000]
        results = await operator_broadcast(f":bar_chart: *{title}*\n{body}")
        if not results:
            return {"pushed": False,
                    "reason": "no operator target configured (OPS_SLACK_CHANNEL / OPS_DISCORD_WEBHOOK)"}
        return {"pushed": any(results.values()), "targets": results}

    async def escalate(args: dict[str, Any]) -> Any:
        """{summary, details?} → report to the operator: durable ops-state entry +
        a push to every configured target (Slack/Discord). The escape hatch for
        anything outside the remediation allow-list."""
        from sportsdata_agents.observability.notify import operator_broadcast

        summary = str(args["summary"])
        state = read_ops_state()
        state.setdefault("escalations", []).append({
            "at": dt.datetime.now(dt.UTC).isoformat(),
            "summary": summary,
            "details": str(args.get("details", ""))[:2000],
        })
        state["escalations"] = state["escalations"][-100:]  # bounded — it's a log, not a DB
        write_ops_state(state)
        results = await operator_broadcast(f":rotating_light: ops escalation: {summary}")
        logger.warning("ops escalation: %s", summary)
        return {"escalated": True, "pushed": any(results.values()) if results else False,
                "targets": results, "state_file": str(ops_state_path())}

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
        _tool("list_repo_files", list_repo_files,
              {"repo": {"type": "string"}, "pattern": {"type": "string"}}, ["repo"]),
        _tool("read_repo_file", read_repo_file,
              {"repo": {"type": "string"}, "path": {"type": "string"}}, ["repo", "path"]),
        _tool("gh_review_pr", gh_review_pr,
              {"repo": {"type": "string"}, "number": {"type": "integer"},
               "verdict": {"type": "string", "enum": ["approve", "request_changes", "comment"]},
               "body": {"type": "string"}},
              ["repo", "number", "verdict"]),
        _tool("site_status", site_status, {}, []),
        _tool("site_audit", site_audit, {}, []),
        _tool("site_traffic", site_traffic, {"days": {"type": "integer"}}, []),
        _tool("post_ops_report", post_ops_report,
              {"title": {"type": "string"}, "body": {"type": "string"}}, ["title"]),
        _tool("propose_change", propose_change,
              {"repo": {"type": "string"}, "branch": {"type": "string"},
               "files": {"type": "array", "items": {
                   "type": "object",
                   "properties": {"path": {"type": "string"}, "content": {"type": "string"},
                                  "find": {"type": "string"}, "replace": {"type": "string"}},
                   "required": ["path"]}},
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
        _tool("delegation_stats", delegation_stats, {"days": {"type": "number"}}, []),
        _tool("alert_quality", alert_quality, {"days": {"type": "number"}}, []),
        _tool("run_offline_evals", run_offline_evals, {}, []),
        _tool("record_agent_metrics", record_agent_metrics,
              {"agent": {"type": "string"}, "runs": {"type": "integer"},
               "success_rate": {"type": "number"}, "cost_per_success_usd": {"type": "number"},
               "avg_latency_ms": {"type": "integer"}, "quality": {"type": "object"}},
              ["agent", "runs"]),
        _tool("escalate", escalate,
              {"summary": {"type": "string"}, "details": {"type": "string"}}, ["summary"]),
    ]
