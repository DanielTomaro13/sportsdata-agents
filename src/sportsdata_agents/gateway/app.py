"""FastAPI gateway (M1.1): the channel-agnostic HTTP door to the team.

Every channel (CLI test client, Slack, web) speaks the same surface:

- ``POST /message``                  — sync run → the answer
- ``POST /message?mode=async``       — task id immediately; poll ``GET /tasks/{id}``
- ``GET  /tasks/{id}/events``        — SSE progress stream (delegations, tool calls)
- ``GET  /agents`` / ``GET /healthz``

Auth is a no-op locally (the §12 seam): tenant/workspace resolve from headers with
local defaults; a per-tenant in-memory rate limiter guards cost. One warm
``TeamSession`` serves the process (the MCP pool + DB recorder live for the app's
lifetime).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from sportsdata_agents.agents.harness import RunResult
from sportsdata_agents.agents.loader import load_builtin_specs
from sportsdata_agents.config import get_settings
from sportsdata_agents.data.repository import TenantScope
from sportsdata_agents.gateway.service import TeamSession, detect_tier_overrides, parsed_sources, try_db_recorder
from sportsdata_agents.gateway.tasks import TaskRecord, TaskStore
from sportsdata_agents.observability.recorder import RunRecorder
from sportsdata_agents.observability.tracing import setup_observability
from sportsdata_agents.workspace import Workspace

logger = logging.getLogger(__name__)

RATE_LIMIT_PER_MINUTE = 30  # per tenant; cost ceilings guard spend, this guards abuse

# The local daemon binds 127.0.0.1, but a Host header isn't the connection address:
# a malicious web page can DNS-rebind its own domain to 127.0.0.1 and drive this API
# from the user's browser (spending their model key, reading their data). Rejecting
# non-local Host headers defeats that — a rebinding page still carries Host=attacker.
# An ABSENT Host is rejected too (browsers always send one; HTTP/1.1 requires it).
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _allowed_hosts() -> frozenset[str]:
    extra = os.environ.get("SPORTSDATA_GATEWAY_ALLOW_HOSTS", "")
    return _LOCAL_HOSTS | {h.strip().lower() for h in extra.split(",") if h.strip()}


def _host_of(header: str) -> str:
    """The hostname from a Host header, dropping the port and any IPv6 brackets."""
    h = (header or "").strip().lower()
    if h.startswith("["):  # [::1]:8765
        return h[1:].split("]", 1)[0]
    return h.rsplit(":", 1)[0] if ":" in h else h


def _secret_configured(name: str) -> bool:
    """Whether a secret resolves WITHOUT prompting — env or the app-private file.
    Deliberately skips the keychain tier so opening Settings can't trigger a macOS
    keychain prompt; the desktop wizard writes keys to the file store anyway."""
    from sportsdata_agents.secrets import get_file_secret

    return bool(os.environ.get(name) or get_file_secret(name))


def _provider_status(provider: str, auth: dict[str, Any]) -> dict[str, Any]:
    """Classify a provider ready / needs_key from its declared auth requirements and
    whether the keys are configured — instant, no network. The geo/bot-blocked case
    isn't detectable without a live probe (a later 'check connectivity' action), so an
    open or already-keyed provider shows 'ready' here."""
    envs = [str(e) for e in (auth.get("auth_env") or [])]
    required = bool(auth.get("auth_required"))
    optional = bool(auth.get("auth_optional"))
    configured = bool(envs) and all(_secret_configured(e) for e in envs)
    return {
        "status": "needs_key" if (required and not configured) else "ready",
        "auth_required": required,
        "auth_optional": optional,
        "auth_env": envs,
        "key_configured": configured,
    }


# ─── request/response models ─────────────────────────────────────────────


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    agent: str | None = None  # run one agent instead of the team
    conversation_id: str | None = None  # channel-side thread key (Slack thread, etc.)


class MessageOut(BaseModel):
    answer: str
    stop_reason: str
    verified: bool | None
    sources: list[str]
    cost_usd: float
    steps: int
    tool_calls: int
    artifacts: list[str] = []  # local file paths tools produced (charts) — channels deliver
    run_id: str | None = None  # link to this turn's trace at /runs/{run_id} (chat trace, B4)


class TaskOut(BaseModel):
    task_id: str
    state: str
    result: MessageOut | None = None
    error: str | None = None


def _to_message_out(result: RunResult) -> MessageOut:
    answer = result.output or "(no answer)"
    parsed = result.parsed
    if parsed is not None and getattr(parsed, "answer", None):
        answer = parsed.answer
    return MessageOut(
        answer=answer,
        stop_reason=result.stop_reason,
        verified=result.verified,
        sources=parsed_sources(result),
        cost_usd=round(result.cost_usd, 6),
        steps=result.steps,
        tool_calls=result.tool_call_count,
        artifacts=list(getattr(result, "artifacts", []) or []),
        run_id=str(result.run_id) if getattr(result, "run_id", None) else None,
    )


# ─── progress: recorder → SSE queue ──────────────────────────────────────


class QueueRecorder:
    """Forwards every hook to ``inner`` and mirrors progress into a task's queue."""

    def __init__(self, queue: asyncio.Queue[dict[str, Any]], inner: RunRecorder | None) -> None:
        self._q = queue
        self.inner = inner

    def usage_sink(self, event: Any) -> None:
        sink = getattr(self.inner, "usage_sink", None)
        if sink is not None:
            sink(event)

    async def on_run_start(self, **kw: Any) -> None:
        await self._q.put({"event": "run_start", "agent": kw.get("agent"), "task": str(kw.get("task"))[:120]})
        if self.inner:
            await self.inner.on_run_start(**kw)

    async def on_tool_call(self, **kw: Any) -> None:
        await self._q.put({"event": "tool_call", "tool": kw.get("tool"), "ok": kw.get("ok")})
        if self.inner:
            await self.inner.on_tool_call(**kw)

    async def on_run_end(self, **kw: Any) -> None:
        await self._q.put({"event": "run_end", "agent": kw.get("agent"), "status": kw.get("status")})
        if self.inner:
            await self.inner.on_run_end(**kw)


# ─── tenancy + rate limiting ─────────────────────────────────────────────


class Tenant(BaseModel):
    tenant_id: str
    workspace_id: str


async def resolve_tenant(
    x_tenant_id: str | None = Header(default=None),
    x_workspace_id: str | None = Header(default=None),
) -> Tenant:
    """Local no-op auth (§12 seam): headers override the configured defaults.
    The desktop model is single-user localhost (the host guard + optional token
    protect the daemon); a future hosted tier replaces this dependency with real
    authentication without touching routes."""
    settings = get_settings()
    return Tenant(
        tenant_id=x_tenant_id or settings.default_tenant,
        workspace_id=x_workspace_id or settings.default_workspace,
    )


class RateLimiter:
    def __init__(self, per_minute: int = RATE_LIMIT_PER_MINUTE) -> None:
        self._per_minute = per_minute
        self._hits: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str) -> None:
        now = time.monotonic()
        hits = [t for t in self._hits[key] if now - t < 60.0]
        if len(hits) >= self._per_minute:
            raise HTTPException(429, detail=f"rate limit: {self._per_minute}/min per tenant")
        hits.append(now)
        self._hits[key] = hits


# ─── app factory ─────────────────────────────────────────────────────────


def create_app(
    *,
    session: TeamSession | None = None,
    conversation_store: Any | None = None,
    demo_only: bool = False,
) -> FastAPI:
    """Build the gateway. ``session``/``conversation_store`` injectable for tests;
    production builds one warm team session (and, when the DB is live, a
    conversation store) for the app lifetime.

    ``demo_only`` exposes nothing but /healthz, /demo/* and /leads — the
    abuse-hardened public surface, and the only mode meant to face the internet.
    The full gateway is the LOCAL desktop daemon: localhost-bound, foreign-Host
    rejected, optional bearer token — but still single-user header-trust, so it
    must not be reverse-proxied to the public internet."""

    state: dict[str, Any] = {"session": session, "convstore": conversation_store}
    tasks = TaskStore()
    limiter = RateLimiter()
    _mcp_cache: dict[str, Any] = {}  # the MCP provider catalogue (5-min TTL)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        owns_session = state["session"] is None
        if owns_session:
            settings = get_settings()
            setup_observability(settings)
            scope = TenantScope(settings.default_tenant, settings.default_workspace)
            recorder = await try_db_recorder(settings, scope)
            workspace = Workspace(
                tenant_id=settings.default_tenant,
                workspace_id=settings.default_workspace,
                model_tiers=detect_tier_overrides(),
            )
            state["session"] = TeamSession(settings=settings, workspace=workspace, recorder=recorder)
            await state["session"].__aenter__()
            if state["convstore"] is None and recorder is not None:
                from sportsdata_agents.gateway.conversations import ConversationStore

                state["convstore"] = ConversationStore(recorder.session_factory, scope)
            logger.info("gateway team session open (%s)", state["session"].agent_name)
        try:
            yield
        finally:
            await tasks.aclose()
            if owns_session and state["session"] is not None:
                await state["session"].__aexit__(None, None, None)

    try:
        from importlib.metadata import version as _pkg_version

        pkg_version = _pkg_version("sportsdata-agents")
    except Exception:  # not installed (e.g. vendored) — cosmetic only
        pkg_version = "0"
    app = FastAPI(title="sportsdata-agents gateway", version=pkg_version, lifespan=lifespan)
    app.state.tasks = tasks

    def current_session() -> TeamSession:
        s = state["session"]
        if s is None:
            raise HTTPException(503, detail="team session not ready")
        return s

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        s = state["session"]
        body = {"ok": s is not None, "agent": s.agent_name if s else None}
        return JSONResponse(body, status_code=200 if s is not None else 503)

    @app.get("/agents")
    async def agents() -> dict[str, Any]:
        """Every agent the install knows, with enough detail for the workbench's
        Agents view to show what each one does and what it can reach."""
        from sportsdata_agents.agents.model_prefs import load_overrides

        pins = load_overrides()
        return {
            spec.id: {
                "display_name": spec.display_name,
                "description": spec.description,
                "tier": spec.model_tier,
                "tier_override": pins.get(spec.id),
                "version": spec.version,
                "plane": getattr(spec, "plane", "product"),
                "capabilities": list(spec.tools.mcp_capabilities),
                "native_tools": list(spec.tools.native),
                "delegates_to": list(spec.can_delegate_to),
                "skills": list(spec.skills),
                "deprecated": spec.deprecated,
            }
            for spec in load_builtin_specs().values()
        }

    def _audit_sessionmaker() -> Any | None:
        """The warm warehouse sessionmaker (from the live recorder, else the app
        engine) for reading run history. None when there's no warehouse."""
        sess = state["session"]
        sf = getattr(getattr(sess, "recorder", None), "session_factory", None)
        if sf is not None:
            return sf
        try:
            from sportsdata_agents.data.db import get_sessionmaker

            return get_sessionmaker()
        except Exception:
            return None

    @app.get("/agents/{agent_id}/runs")
    async def agent_runs(agent_id: str) -> dict[str, Any]:
        """An agent's recent activity — what it's been working on (M4.5). Newest first."""
        from sqlalchemy import select

        from sportsdata_agents.data.models import AgentRun

        sf = _audit_sessionmaker()
        if sf is None:
            return {"runs": []}
        s = get_settings()
        try:
            async with sf() as session:
                rows = (
                    (
                        await session.execute(
                            select(AgentRun)
                            .where(
                                AgentRun.agent == agent_id,
                                AgentRun.tenant_id == s.default_tenant,
                                AgentRun.workspace_id == s.default_workspace,
                            )
                            .order_by(AgentRun.created_at.desc())
                            .limit(40)
                        )
                    )
                    .scalars()
                    .all()
                )
        except Exception as e:  # warehouse hiccup → empty, not a 500
            logger.warning("agent runs unavailable (%s: %s)", type(e).__name__, e)
            return {"runs": []}
        return {
            "runs": [
                {
                    "id": str(r.id),
                    "status": r.status,
                    "task": r.input_task,
                    "cost_usd": float(r.cost_usd or 0),
                    "tokens_in": r.tokens_in,
                    "tokens_out": r.tokens_out,
                    "latency_ms": r.latency_ms,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "is_delegation": r.parent_run_id is not None,
                }
                for r in rows
            ]
        }

    @app.get("/alerts")
    async def alerts(kind: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Recent fired monitor alerts (arb / line_move / value) for the Monitors pane —
        the platform's edges in one feed. Newest first; optional ?kind= filter. Empty (not
        a 500) when the warehouse is unreachable."""
        from sqlalchemy import select

        from sportsdata_agents.data.models import Alert

        sf = _audit_sessionmaker()
        if sf is None:
            return {"alerts": []}
        s = get_settings()
        lim = max(1, min(int(limit or 50), 200))
        try:
            async with sf() as session:
                q = select(Alert).where(
                    Alert.tenant_id == s.default_tenant,
                    Alert.workspace_id == s.default_workspace,
                )
                if kind:
                    q = q.where(Alert.kind == kind)
                rows = (
                    (await session.execute(q.order_by(Alert.created_at.desc()).limit(lim)))
                    .scalars()
                    .all()
                )
        except Exception as e:  # warehouse hiccup → empty, not a 500
            logger.warning("alerts unavailable (%s: %s)", type(e).__name__, e)
            return {"alerts": []}
        return {
            "alerts": [
                {
                    "id": str(a.id),
                    "kind": a.kind,
                    "message": a.message,
                    "payload": a.payload or {},
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                }
                for a in rows
            ]
        }

    @app.get("/runs/{run_id}")
    async def run_trace(run_id: str) -> JSONResponse:
        """One run's full trace: its transcript (the model's reasoning + tool results),
        the tool calls it made, and the sub-agents it delegated to (M4.5)."""
        import uuid as _uuid

        from sqlalchemy import select

        from sportsdata_agents.data.models import AgentRun, RunTranscript, ToolCall

        sf = _audit_sessionmaker()
        if sf is None:
            return JSONResponse({"detail": "no warehouse"}, status_code=503)
        try:
            rid = _uuid.UUID(run_id)
        except ValueError:
            return JSONResponse({"detail": "bad run id"}, status_code=400)
        s = get_settings()
        async with sf() as session:
            run = await session.get(AgentRun, rid)
            if run is None or run.tenant_id != s.default_tenant or run.workspace_id != s.default_workspace:
                return JSONResponse({"detail": "unknown run"}, status_code=404)
            tx = await session.scalar(select(RunTranscript).where(RunTranscript.agent_run_id == rid))
            tools = (
                (await session.execute(
                    select(ToolCall).where(ToolCall.agent_run_id == rid).order_by(ToolCall.created_at.asc())
                )).scalars().all()
            )
            children = (
                (await session.execute(
                    select(AgentRun).where(AgentRun.parent_run_id == rid).order_by(AgentRun.created_at.asc())
                )).scalars().all()
            )
        return JSONResponse({
            "id": str(run.id),
            "agent": run.agent,
            "status": run.status,
            "task": run.input_task,
            "cost_usd": float(run.cost_usd or 0),
            "tokens_in": run.tokens_in,
            "tokens_out": run.tokens_out,
            "error": run.error,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "transcript": (tx.messages if tx else []),
            "tool_calls": [
                {"tool": t.tool, "args": t.args, "ok": t.ok, "latency_ms": t.latency_ms}
                for t in tools
            ],
            "delegations": [
                {"id": str(c.id), "agent": c.agent, "task": c.input_task, "status": c.status}
                for c in children
            ],
        })

    def _account_payload() -> dict[str, Any]:
        from sportsdata_agents.licensing import current_entitlements

        ent = current_entitlements()
        return {
            "tier": ent.tier,
            "mcp_quota": ent.effective_mcp_quota(),
            "chat_ui": ent.chat_ui,
            "full_app": ent.full_app,
            "agents": "all" if ent.agents is None else list(ent.agents),
            "addons": sorted(ent.addons),
            "seats": ent.seats,
            "note": ent.note,
            "version": pkg_version,
        }

    @app.get("/account")
    async def account() -> dict[str, Any]:
        """The running install's tier + entitlements + where to upgrade — so the UI
        can show the plan and offer a one-click upgrade."""
        return _account_payload()

    # (The B6 marketplace/storefront routes lived here until the platform went free
    # and open source — the app no longer sells anything.)

    @app.get("/skills")
    async def skills_route() -> dict[str, Any]:
        """Every skill the platform knows — built-in + learned — so the UI can show
        what the generalist has grown (learned entries carry a recall count)."""
        from sportsdata_agents.tools.skillsmith import list_skills

        return await list_skills({})

    @app.post("/skills/remove")
    async def skills_remove(body: dict[str, Any]) -> JSONResponse:
        """Prune one LEARNED skill — the user clicking remove in the UI. Built-ins
        are protected; deletion is deliberately user-initiated, never an agent tool."""
        from sportsdata_agents.tools.skillsmith import remove_skill

        try:
            res = remove_skill(str(body.get("name", "")))
        except ValueError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)
        return JSONResponse(res)

    # ─── workbench: chat history, files, settings (M4.5) ────────────────────
    # Read-only surfaces the desktop shell renders as its Chats / Files / Settings
    # panes. All degrade to empty (never 500) so a missing warehouse or data plane
    # just shows an empty pane, matching the degradation contract elsewhere.

    @app.get("/conversations")
    async def list_conversations(include_archived: bool = False) -> dict[str, Any]:
        """The chat-history sidebar: past 'web' conversations, newest first.
        ``?include_archived=1`` also returns archived threads (each carries its flag)."""
        store = state["convstore"]
        if store is None:
            return {"conversations": []}
        try:
            return {"conversations": await store.list_conversations(include_archived=include_archived)}
        except Exception as e:  # warehouse hiccup → empty history, not a 500
            logger.warning("conversation list unavailable (%s: %s)", type(e).__name__, e)
            return {"conversations": []}

    @app.post("/conversations/{key}/archive")
    async def archive_conversation(key: str, body: dict[str, Any]) -> JSONResponse:
        """Archive (hide from the sidebar) or unarchive a conversation."""
        store = state["convstore"]
        if store is None:
            return JSONResponse({"detail": "no conversation store"}, status_code=503)
        ok = await store.set_archived(key, bool(body.get("archived", True)))
        if not ok:
            return JSONResponse({"detail": "unknown conversation"}, status_code=404)
        return JSONResponse({"ok": True})

    @app.post("/conversations/{key}/rename")
    async def rename_conversation(key: str, body: dict[str, Any]) -> JSONResponse:
        """Set a custom title for a conversation (overrides the first-message title)."""
        store = state["convstore"]
        if store is None:
            return JSONResponse({"detail": "no conversation store"}, status_code=503)
        title = str(body.get("title", "")).strip()
        if not title:
            return JSONResponse({"detail": "a title is required"}, status_code=422)
        ok = await store.set_title(key, title)
        if not ok:
            return JSONResponse({"detail": "unknown conversation"}, status_code=404)
        return JSONResponse({"ok": True})

    @app.get("/conversations/{key}/settings")
    async def conversation_settings(key: str) -> JSONResponse:
        """A conversation's model + provider scope (workbench B2)."""
        store = state["convstore"]
        if store is None:
            return JSONResponse({"detail": "no conversation store"}, status_code=503)
        try:
            settings = await store.settings_for(key)
        except Exception as e:  # warehouse down/unmigrated → defaults, not a 500
            logger.warning("conversation settings read failed (%s: %s)", type(e).__name__, e)
            settings = None
        if settings is None:
            # An unsaved (brand-new) chat simply has defaults — not an error.
            settings = {"model_tier": None, "mcp_providers": None}
        return JSONResponse(settings)

    @app.post("/conversations/{key}/settings")
    async def set_conversation_settings(key: str, body: dict[str, Any]) -> JSONResponse:
        """Set a conversation's forced model and/or provider scope (workbench B2).
        Body: ``{model_tier: str|null, mcp_providers: [str]|null}`` — null clears
        back to defaults. Narrow-only: the scope can hide licensed providers from
        this chat, never grant unlicensed ones."""
        store = state["convstore"]
        if store is None:
            return JSONResponse({"detail": "no conversation store"}, status_code=503)
        from sportsdata_agents.agents.model_prefs import _valid_tier

        tier = body.get("model_tier")
        tier = str(tier).strip() if tier else None
        if tier and not _valid_tier(tier):
            raise HTTPException(400, detail=f"model_tier {tier!r} must be a tier name or 'provider/model'")
        providers = body.get("mcp_providers")
        if providers is not None:
            if not isinstance(providers, list) or not all(isinstance(p, str) and p.strip() for p in providers):
                raise HTTPException(400, detail="mcp_providers must be a list of provider ids, or null")
            providers = [p.strip() for p in providers]
        try:
            ok = await store.set_settings(key, model_tier=tier, mcp_providers=providers)
        except Exception as e:  # warehouse down/unmigrated → a clear 503, not a 500
            logger.warning("conversation settings write failed (%s: %s)", type(e).__name__, e)
            ok = False
        if not ok:
            return JSONResponse({"detail": "could not persist settings"}, status_code=503)
        return JSONResponse({"ok": True, "model_tier": tier, "mcp_providers": providers})

    @app.delete("/conversations/{key}")
    async def delete_conversation(key: str) -> JSONResponse:
        """Permanently delete a conversation and its messages."""
        store = state["convstore"]
        if store is None:
            return JSONResponse({"detail": "no conversation store"}, status_code=503)
        ok = await store.delete_conversation(key)
        if not ok:
            return JSONResponse({"detail": "unknown conversation"}, status_code=404)
        return JSONResponse({"ok": True})

    @app.get("/conversations/{key}/messages")
    async def conversation_messages(key: str) -> JSONResponse:
        """Reload one past conversation's turns (oldest first)."""
        store = state["convstore"]
        if store is None:
            return JSONResponse({"messages": []})
        try:
            msgs = await store.messages_for(key)
        except Exception as e:
            logger.warning("conversation load failed (%s: %s)", type(e).__name__, e)
            return JSONResponse({"messages": []})
        if msgs is None:
            return JSONResponse({"detail": "unknown conversation"}, status_code=404)
        return JSONResponse({"messages": msgs})

    @app.get("/files")
    async def list_desk_files() -> dict[str, Any]:
        """The Files pane: everything agents have written to the user's desk folder
        (charts, CSVs, reports), newest first."""
        from datetime import UTC, datetime

        from sportsdata_agents.paths import desk_dir

        base = desk_dir()
        files: list[dict[str, Any]] = []
        if base.is_dir():
            for p in base.rglob("*"):
                if not p.is_file() or p.name.startswith("."):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                files.append({
                    "name": str(p.relative_to(base)),
                    "size": st.st_size,
                    "modified": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(),
                    "ext": p.suffix.lower().lstrip("."),
                })
        files.sort(key=lambda f: f["modified"], reverse=True)
        return {"desk_dir": str(base), "files": files}

    @app.get("/files/raw")
    async def desk_file_raw(name: str):
        """Serve one desk file for preview/download — sandboxed to the desk folder
        (``resolve_desk_path`` rejects any path that escapes it)."""
        from fastapi.responses import FileResponse

        from sportsdata_agents.paths import resolve_desk_path

        try:
            path = resolve_desk_path(name)
        except ValueError:
            raise HTTPException(400, detail="bad path") from None
        if not path.is_file():
            raise HTTPException(404, detail="not found")
        # no forced-download filename → images/text/PDF preview inline in a new tab
        return FileResponse(str(path))

    @app.get("/settings")
    async def settings_snapshot() -> dict[str, Any]:
        """The Settings pane: where data lives, which model provider is configured,
        and the desk folder — a read-only snapshot (mutation lands in a later PR)."""
        from sportsdata_agents.app.wizard import configured_provider
        from sportsdata_agents.paths import data_dir, desk_dir, warehouse_path

        provider = None
        try:
            provider = configured_provider()  # only returns a provider that HAS a key
        except Exception as e:  # never let a config probe 500 the pane
            logger.warning("provider probe failed (%s: %s)", type(e).__name__, e)
        sess = state["session"]
        return {
            "provider": provider.label if provider else None,
            "model_key_configured": provider is not None,
            "root_agent": sess.agent_name if sess else None,
            "data_dir": str(data_dir()),
            "warehouse": str(warehouse_path()),
            "desk_dir": str(desk_dir()),
            "account": _account_payload(),
        }

    @app.get("/mcp/groups")
    async def mcp_groups() -> JSONResponse:
        """The MCP provider catalogue (groups + tool counts) from the live data
        plane. Cached for 5 min — it moves slowly and the call spawns a subprocess.
        Empty payload (not an error) when the data plane is unreachable."""
        return JSONResponse(_annotate_enabled(await _mcp_groups_payload()))

    async def _mcp_groups_payload() -> dict[str, Any]:
        """The (cached) raw provider catalogue — shared by the route and the B2
        per-conversation scope, which needs the provider universe to turn an
        allowed-list into a deny-set."""
        cached = _mcp_cache.get("groups")
        if cached and time.monotonic() - _mcp_cache.get("at", 0.0) < 300:
            return cached
        payload: dict[str, Any] = {"providers": [], "available": {}}
        try:
            from sportsdata_agents.mcp.manager import MCPManager

            async with MCPManager(groups=["*"], command=get_settings().mcp_command) as manager:
                got = await asyncio.wait_for(manager.call_tool("list_available_groups", {}), timeout=20)
            available = got.get("available") or {}
            auth = got.get("providers") or {}  # provider → {auth_env, auth_required, auth_optional}
            by_provider: dict[str, dict[str, Any]] = {}
            for group, info in available.items():
                prov = str(info.get("provider", group.split(".")[0]))
                entry = by_provider.setdefault(prov, {"provider": prov, "groups": [], "tools": 0})
                entry["groups"].append({"group": group, "tools": int(info.get("tools", 0))})
                entry["tools"] += int(info.get("tools", 0))
            for prov, entry in by_provider.items():
                entry.update(_provider_status(prov, auth.get(prov, {})))
            payload = {
                "providers": sorted(by_provider.values(), key=lambda e: e["provider"]),
                "available": available,
            }
            _mcp_cache.update(groups=payload, at=time.monotonic())
        except Exception as e:  # data plane down / slow → empty catalogue, not a 500
            logger.warning("mcp group listing unavailable (%s: %s)", type(e).__name__, e)
        return payload

    async def _conv_deny(allowed: list[str] | None) -> frozenset[str] | None:
        """The B2 deny-set for a conversation's allowed-provider list: the licensed
        universe minus the list. Degrades OPEN (no scope, with a warning) when the
        data plane can't enumerate providers — the scope is a UX convenience; the
        licence gate and the B1 off-switch stay the hard boundaries."""
        if not allowed:
            return None
        universe = {
            str(p.get("provider"))
            for p in (await _mcp_groups_payload()).get("providers") or []
        }
        if not universe:
            logger.warning("conversation scope skipped — provider universe unavailable")
            return None
        return frozenset(universe - set(allowed)) or None

    def _annotate_enabled(payload: dict[str, Any]) -> dict[str, Any]:
        """Stamp each provider's live on/off flag (computed OUTSIDE the 5-min catalogue cache
        so a toggle is reflected immediately). Workbench B1."""
        from sportsdata_agents.mcp.prefs import load_disabled

        disabled = load_disabled()
        for p in payload.get("providers") or []:
            p["enabled"] = p.get("provider") not in disabled
        return payload

    @app.post("/mcp/toggle")
    async def mcp_toggle(body: dict[str, Any]) -> JSONResponse:
        """Globally enable/disable a data provider (workbench B1). Persists to the data dir;
        takes effect on the next agent run (the disabled set is passed to the MCP subprocess).
        Body: ``{provider: str, enabled: bool}``."""
        from sportsdata_agents.mcp.prefs import set_disabled

        provider = str(body.get("provider", "")).strip()
        if not provider:
            raise HTTPException(400, detail="provider is required")
        enabled = bool(body.get("enabled", True))
        set_disabled(provider, disabled=not enabled)
        return JSONResponse({"provider": provider, "enabled": enabled})

    @app.post("/agents/model")
    async def agent_model(body: dict[str, Any]) -> JSONResponse:
        """Pin (or clear) an agent's model (workbench B3). Body: ``{agent: str,
        tier: str|null}`` — a tier name, an explicit "provider/model", or null/"" to
        clear back to the spec default. Takes effect on the next run."""
        from sportsdata_agents.agents.model_prefs import _valid_tier, set_override

        agent_id = str(body.get("agent", "")).strip()
        if agent_id not in load_builtin_specs():
            raise HTTPException(404, detail=f"unknown agent {agent_id!r}")
        tier = body.get("tier")
        tier = str(tier).strip() if tier is not None else None
        if tier and not _valid_tier(tier):
            raise HTTPException(400, detail=f"tier {tier!r} must be a tier name or 'provider/model'")
        pins = set_override(agent_id, tier or None)
        return JSONResponse({"agent": agent_id, "tier_override": pins.get(agent_id)})

    # ─── the operator panel (owner-only: 404 for everyone but the operator) ───
    # The same switch that gates the platform-maintenance jobs gates this surface
    # (is_operator → a signed operator licence claim on a release build), so only
    # the product owner's deployment ever serves it. Customers' installs return
    # 404 — the panel doesn't exist for them, and a forged env var won't reveal it.

    def _operator_only() -> JSONResponse | None:
        from sportsdata_agents.operations.scheduler import is_operator

        if not is_operator():
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return None

    @app.get("/operator/overview")
    async def operator_overview() -> JSONResponse:
        """Everything the operator console shows, in one payload: config preflight,
        spend + budget, and ops-plane status — the CLI commands, app-shaped."""
        denied = _operator_only()
        if denied:
            return denied
        from sportsdata_agents.data.db import make_engine, make_sessionmaker
        from sportsdata_agents.operations import costs as cost_mod
        from sportsdata_agents.operations.preflight import run_preflight, summarise
        from sportsdata_agents.operations.scheduler import status as job_status
        from sportsdata_agents.tools.ops import read_ops_state

        checks = [c.__dict__ for c in run_preflight()]
        spend: dict[str, Any] | None = None
        budget: dict[str, Any] | None = None
        try:
            engine = make_engine(get_settings().database_url)
            try:
                sf = make_sessionmaker(engine)
                spend = await cost_mod.spend_report(sf, days=7)
                budget = await cost_mod.budget_status(sf)
            finally:
                await engine.dispose()
        except Exception as e:  # DB down: the panel still shows config + ops state
            logger.warning("operator overview: spend unavailable (%s)", e)
        if budget is None:
            # the budget CONFIG is a local file — show the cap even when the
            # warehouse (and so the spent figure) is unreachable
            configured = cost_mod.get_budget()
            if configured:
                budget = {**configured, "spent_usd": None, "pct": None, "breached": False}
        ops_state = read_ops_state()
        ops_agents = sorted(
            sid for sid, s in load_builtin_specs().items()
            if getattr(s, "plane", "product") == "ops"
        )
        return JSONResponse({
            "preflight": {"checks": checks, "summary": summarise(run_preflight())},
            "costs": spend,
            "budget": budget,
            "ops": {
                "escalations": (ops_state.get("escalations") or [])[-5:],
                "disabled_feeds": ops_state.get("disabled_feeds") or [],
                "jobs": job_status(),
                "agents": ops_agents,
            },
        })

    @app.post("/operator/budget")
    async def operator_budget(body: dict[str, Any]) -> JSONResponse:
        """Set the spend budget from the panel (same as `agents costs --set-budget`)."""
        denied = _operator_only()
        if denied:
            return denied
        from sportsdata_agents.operations import costs as cost_mod

        try:
            budget = cost_mod.set_budget(float(body.get("cap_usd", 0)),
                                         str(body.get("period", "monthly")))
        except (TypeError, ValueError) as e:
            return JSONResponse({"detail": str(e)}, status_code=422)
        return JSONResponse({"ok": True, "budget": budget})

    @app.post("/operator/actions/health")
    async def operator_run_health() -> JSONResponse:
        """Run the deterministic platform health check (doctor + feeds + site) on
        demand — the same check the `ops_health` conductor job runs, returned inline."""
        denied = _operator_only()
        if denied:
            return denied
        from sportsdata_agents.data.db import make_engine, make_sessionmaker
        from sportsdata_agents.operations.health import run_health

        engine = make_engine(get_settings().database_url)
        try:
            health = await run_health(make_sessionmaker(engine))
        except Exception as e:  # warehouse/site unreachable: report, don't 500
            logger.warning("operator health action failed (%s)", e)
            return JSONResponse({"detail": f"health check failed: {e}"}, status_code=503)
        finally:
            await engine.dispose()
        return JSONResponse({"ok": True, "health": health})

    @app.post("/operator/actions/run-ops")
    async def operator_run_ops(body: dict[str, Any]) -> JSONResponse:
        """Trigger an ops-plane agent run — the operator's full trigger. Spawns the
        same `agents ops run <agent> <prompt>` the conductor uses, detached; the
        result lands in the ops run history (visible on the next overview refresh),
        not this response. The agent must be a known ops-plane agent."""
        denied = _operator_only()
        if denied:
            return denied
        import subprocess

        from sportsdata_agents.operations.scheduler import _agents_binary

        agent = str(body.get("agent", "")).strip()
        prompt = str(body.get("prompt", "")).strip()
        ops_agents = {
            sid for sid, s in load_builtin_specs().items()
            if getattr(s, "plane", "product") == "ops"
        }
        if agent not in ops_agents:
            return JSONResponse(
                {"detail": f"unknown ops agent {agent!r}", "ops_agents": sorted(ops_agents)},
                status_code=422,
            )
        if not prompt:
            return JSONResponse({"detail": "an instruction/prompt is required"}, status_code=422)
        # argv (not shell) — `prompt` can't inject; `agent` is allow-listed above.
        # start_new_session detaches it so it outlives this request and is reaped by init.
        try:
            # argv form (not a shell string) + allow-listed agent ⇒ no injection surface.
            subprocess.Popen(
                [_agents_binary(), "ops", "run", agent, prompt],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            )
        except Exception as e:
            return JSONResponse({"detail": f"could not start ops run: {e}"}, status_code=503)
        return JSONResponse({"ok": True, "started": True, "agent": agent})

    @app.post("/message", response_model=None)
    async def message(
        body: MessageIn,
        request: Request,
        tenant: Tenant = Depends(resolve_tenant),
    ) -> MessageOut | TaskOut:
        limiter.check(tenant.tenant_id)
        session = current_session()
        # Per-request agent override runs through the same warm session's team when
        # possible; a different single agent would need its own session (kept simple:
        # the shared session's root answers; body.agent is honoured when it matches).
        if body.agent and body.agent != session.agent_name:
            raise HTTPException(400, detail=f"this gateway serves {session.agent_name!r}; start one per agent")

        # Conversation threading: prior turns prefix the prompt; storage failures
        # degrade to a stateless turn, never a failed request.
        from sportsdata_agents.gateway.conversations import threaded_prompt

        convkey, store = body.conversation_id, state["convstore"]
        prompt = body.text
        conv_tier: str | None = None
        conv_deny: frozenset[str] | None = None
        if convkey and store is not None:
            try:
                prompt = threaded_prompt(await store.context_for(convkey), body.text)
            except Exception as e:
                logger.warning("conversation context unavailable (%s: %s)", type(e).__name__, e)
            # B2 per-conversation settings ride this run: the forced model, and the
            # provider scope as a deny-set (settings failures degrade to defaults).
            try:
                settings = await store.settings_for(convkey) or {}
                conv_tier = settings.get("model_tier") or None
                conv_deny = await _conv_deny(settings.get("mcp_providers"))
            except Exception as e:
                logger.warning("conversation settings unavailable (%s: %s)", type(e).__name__, e)
        # Passed only when set — a plain session without the B2 kwargs keeps working.
        run_kw: dict[str, Any] = {}
        if conv_tier:
            run_kw["tier"] = conv_tier
        if conv_deny:
            run_kw["mcp_deny"] = conv_deny

        async def remember_turn(out: MessageOut) -> None:
            if convkey and store is not None:
                try:
                    await store.append_turn(convkey, body.text, out.answer)
                except Exception as e:
                    logger.warning("conversation append failed (%s: %s)", type(e).__name__, e)

        if request.query_params.get("mode") == "async":
            def factory(record: TaskRecord):
                async def run() -> MessageOut:
                    # Mirror progress into the task's queue (and the DB recorder).
                    # Per-run override — never mutate the shared session's harness:
                    # concurrent requests would race on it.
                    mirror = QueueRecorder(record.events, getattr(session, "recorder", None))
                    result = await session.run(prompt, recorder=mirror, **run_kw)
                    out = _to_message_out(result)
                    await remember_turn(out)
                    return out

                return run()

            record = tasks.submit(factory)
            return TaskOut(task_id=record.id, state=record.state)

        result = await session.run(prompt, **run_kw)
        out = _to_message_out(result)
        await remember_turn(out)
        return out

    @app.get("/tasks/{task_id}")
    async def task_status(task_id: str) -> TaskOut:
        record = tasks.get(task_id)
        if record is None:
            raise HTTPException(404, detail="unknown task id")
        return TaskOut(
            task_id=record.id,
            state=record.state,
            result=record.result if isinstance(record.result, MessageOut) else None,
            error=record.error,
        )

    @app.get("/tasks/{task_id}/events")
    async def task_events(task_id: str) -> StreamingResponse:
        record = tasks.get(task_id)
        if record is None:
            raise HTTPException(404, detail="unknown task id")

        async def stream():
            while True:
                # Late joiners (or a reconnect after the end marker was consumed)
                # must not hang on keepalives for a task that already finished.
                if record.events.empty() and record.state in ("done", "error"):
                    yield f"data: {json.dumps({'event': 'end', 'state': record.state})}\n\n"
                    return
                try:
                    event = await asyncio.wait_for(record.events.get(), timeout=30.0)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("event") == "end":
                    return

        return StreamingResponse(stream(), media_type="text/event-stream")

    # ─── public demo + leads (M3.4, D22) ────────────────────────────────
    demo_limiter = RateLimiter(per_minute=3)
    demo_stats_cache: dict[str, Any] = {}
    # global cap: per-IP limits don't stop N different IPs each spawning a team
    # session + MCP subprocesses (audit finding) — beyond this, 429 immediately
    demo_slots = asyncio.Semaphore(2)

    @app.get("/demo/prompts")
    async def demo_prompts() -> dict[str, Any]:
        from sportsdata_agents.gateway.demo import DEMO_PROMPTS

        return {"prompts": [{"id": p["id"], "title": p["title"]} for p in DEMO_PROMPTS]}

    @app.post("/demo/run")
    async def demo_run(body: dict[str, Any], request: Request) -> dict[str, Any]:
        """Run ONE curated demo prompt (free-form input deliberately does not
        exist — D22's abuse posture). Per-IP rate limited; tiny per-run budget."""
        from sportsdata_agents.gateway.demo import run_demo

        client = request.client.host if request.client else "unknown"
        demo_limiter.check(f"demo:{client}")
        prompt_id = str(body.get("prompt_id", ""))
        if demo_slots.locked():
            raise HTTPException(429, detail="demo at capacity — try again in a minute")
        async with demo_slots:
            try:
                return await run_demo(prompt_id)
            except KeyError:
                raise HTTPException(404, detail=f"unknown demo prompt {prompt_id!r}") from None

    @app.get("/demo/stats")
    async def demo_stats_route() -> dict[str, Any]:
        """Live capability counters (cached for an hour — they move slowly)."""
        from sportsdata_agents.gateway.demo import demo_stats

        cached = demo_stats_cache.get("stats")
        if cached and time.monotonic() - demo_stats_cache.get("at", 0.0) < 3600:
            return cached
        stats = await demo_stats()
        demo_stats_cache.update(stats=stats, at=time.monotonic())
        return stats

    @app.post("/leads")
    async def create_lead(body: dict[str, Any], request: Request) -> dict[str, Any]:
        """Marketing-site lead capture. DB row when the database is up; an
        append-only local file otherwise — a lead must never be lost."""
        email = str(body.get("email", "")).strip()
        if "@" not in email or "." not in email.split("@")[-1] or len(email) > 320:
            raise HTTPException(422, detail="a valid email is required")
        client = request.client.host if request.client else "unknown"
        limiter.check(f"leads:{client}")
        note = str(body.get("note", ""))[:1000]
        try:
            from sportsdata_agents.data.db import get_sessionmaker
            from sportsdata_agents.data.models import Lead

            async with get_sessionmaker()() as db:
                db.add(Lead(email=email, note=note, source=str(body.get("source", "site"))[:64]))
                await db.commit()
            return {"ok": True, "stored": "db"}
        except Exception:
            import json as _json

            from sportsdata_agents.paths import data_dir

            fallback = data_dir() / "leads.jsonl"
            with fallback.open("a", encoding="utf-8") as fh:
                fh.write(_json.dumps({"email": email, "note": note}) + "\n")
            return {"ok": True, "stored": "file"}

    @app.post("/conversations/{conversation_id}/message", response_model=None)
    async def conversation_message(
        conversation_id: str,
        body: MessageIn,
        request: Request,
        tenant: Tenant = Depends(resolve_tenant),
    ) -> MessageOut | TaskOut:
        """Channel-thread entry point (Slack threads map here). Turns are currently
        independent — the conversation key is accepted but no transcript is threaded
        back into context yet (P2 backlog; durable facts flow via remember/recall)."""
        body.conversation_id = conversation_id
        return await message(body, request, tenant)

    # ─── local-daemon hardening (P4): the non-demo gateway is localhost-only ───
    # demo_only is the deliberately-public surface (its own gate below); the desktop
    # daemon must NOT be drivable from a web page via DNS rebinding. We reject foreign
    # Host headers and, when SPORTSDATA_GATEWAY_TOKEN is set, require it on mutating
    # requests (defense-in-depth against other local processes). /healthz stays open
    # for the .app launcher's readiness probe.
    if not demo_only:
        @app.middleware("http")
        async def _local_guard(request: Request, call_next: Any) -> Any:
            if request.url.path != "/healthz":
                if _host_of(request.headers.get("host", "")) not in _allowed_hosts():
                    return JSONResponse({"detail": "forbidden host"}, status_code=403)
                token = os.environ.get("SPORTSDATA_GATEWAY_TOKEN")
                if token and request.method not in ("GET", "HEAD", "OPTIONS"):
                    # header only (query strings leak into access logs); constant-time
                    # compare so a local probe can't walk the token byte-by-byte
                    import hmac as _hmac

                    sent = request.headers.get("x-sportsdata-token") or ""
                    if not _hmac.compare_digest(sent, token):
                        return JSONResponse({"detail": "missing or invalid token"}, status_code=401)
            return await call_next(request)

    # ─── the web chat UI (M4.2) — served at / when not a public demo node ───
    # The full chat surface is a paid feature; gate it the same way the CLI gates
    # `agents serve`. demo_only nodes never serve it (they're public).
    if not demo_only:
        from pathlib import Path

        from fastapi.staticfiles import StaticFiles

        from sportsdata_agents.licensing.enforce import EntitlementError, require_chat_ui

        class _NoCacheStatic(StaticFiles):
            """Serve the UI with ``Cache-Control: no-store`` so a client (browser or
            the desktop window's web view) never shows a STALE page — a cached
            operator chip or an old build kept reappearing otherwise. It's a
            localhost app, so there's no bandwidth cost to always-fresh."""

            async def get_response(self, path: str, scope: Any) -> Any:
                response = await super().get_response(path, scope)
                response.headers["Cache-Control"] = "no-store, must-revalidate"
                return response

        ui_dir = Path(__file__).parent / "ui"
        try:
            require_chat_ui()
            if ui_dir.is_dir():
                app.mount("/", _NoCacheStatic(directory=str(ui_dir), html=True), name="ui")
        except EntitlementError:
            logger.info("chat UI not served: the tier doesn't include it (API still available)")

    if demo_only:
        _public = ("/healthz", "/demo", "/leads")

        @app.middleware("http")
        async def _demo_gate(request: Request, call_next: Any) -> Any:
            path = request.url.path
            if not any(path == p or path.startswith(p + "/") for p in _public):
                from fastapi.responses import JSONResponse

                return JSONResponse({"detail": "Not Found"}, status_code=404)
            return await call_next(request)

    return app


def serve(host: str = "127.0.0.1", port: int = 8400, demo_only: bool = False) -> None:
    import uvicorn

    uvicorn.run(create_app(demo_only=demo_only), host=host, port=port)
