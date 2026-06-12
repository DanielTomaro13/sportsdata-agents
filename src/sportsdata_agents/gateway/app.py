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
    Real authentication replaces this dependency at P4 without touching routes."""
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
    abuse-hardened public surface. Until P4 replaces resolve_tenant with real
    auth, the full gateway trusts headers and must not face the internet."""

    state: dict[str, Any] = {"session": session, "convstore": conversation_store}
    tasks = TaskStore()
    limiter = RateLimiter()

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
        return {
            spec.id: {"display_name": spec.display_name, "tier": spec.model_tier, "version": spec.version}
            for spec in load_builtin_specs().values()
        }

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
            "upgrade_url": os.environ.get(
                "SPORTSDATA_UPGRADE_URL",
                "https://danieltomaro13.github.io/sportsdata-site/#pricing",
            ),
        }

    @app.get("/account")
    async def account() -> dict[str, Any]:
        """The running install's tier + entitlements + where to upgrade — so the UI
        can show the plan and offer a one-click upgrade."""
        return _account_payload()

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

    @app.post("/account/activate")
    async def activate(body: dict[str, Any]) -> JSONResponse:
        """Activate (or upgrade to) a licence key from the UI: verify it, store it in
        the OS keychain, and return the refreshed entitlements. Self-serve upgrade —
        buy → paste the emailed key → instantly on the new tier."""
        from sportsdata_agents.licensing import verify_license
        from sportsdata_agents.licensing.license import KEYCHAIN_LICENSE_NAME
        from sportsdata_agents.secrets import set_keychain_secret

        key = str(body.get("key", "")).strip()
        if not key:
            return JSONResponse({"detail": "a licence key is required"}, status_code=422)
        try:
            claims = verify_license(key)
        except Exception as e:  # bad signature / expired / no pubkey baked (dev build)
            return JSONResponse({"detail": f"that key did not verify: {e}"}, status_code=400)
        if not set_keychain_secret(KEYCHAIN_LICENSE_NAME, key):
            from sportsdata_agents.paths import data_dir

            (data_dir() / "license.key").write_text(key, encoding="utf-8")
        return JSONResponse({"ok": True, "issued_to": claims.issued_to, "account": _account_payload()})

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
        if convkey and store is not None:
            try:
                prompt = threaded_prompt(await store.context_for(convkey), body.text)
            except Exception as e:
                logger.warning("conversation context unavailable (%s: %s)", type(e).__name__, e)

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
                    result = await session.run(prompt, recorder=mirror)
                    out = _to_message_out(result)
                    await remember_turn(out)
                    return out

                return run()

            record = tasks.submit(factory)
            return TaskOut(task_id=record.id, state=record.state)

        result = await session.run(prompt)
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

        ui_dir = Path(__file__).parent / "ui"
        try:
            require_chat_ui()
            if ui_dir.is_dir():
                app.mount("/", StaticFiles(directory=str(ui_dir), html=True), name="ui")
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
