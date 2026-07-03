"""Watch-management session tools (M3.2) — agents create and inspect standing
watches; the deterministic monitor engine (cron'd `agents monitor`) does the
firing. Tenant-scoped like every customer table."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.agents.harness import ToolDef
from sportsdata_agents.data.models import Alert, Subscription
from sportsdata_agents.data.repository import TenantScope

MONITOR_TOOL_NAMES = {"create_watch", "list_watches", "delete_watch", "list_alerts"}

_KINDS = ("line_move", "steam", "value", "scratching", "arb", "model_value")


def monitoring_tools(
    session_factory: async_sessionmaker[AsyncSession], scope: TenantScope
) -> list[ToolDef]:
    async def create_watch(args: dict[str, Any]) -> Any:
        """{name, kind: line_move|steam|value|scratching|arb|model_value, params?,
        channel?} → a standing watch the monitor engine evaluates each cycle.
        params per kind: line_move {threshold_pct, sport?, market?, selection?,
        book?}; steam {min_moves, ...same filters}; value {min_edge_pct};
        scratching {stale_minutes, sport?}; model_value {sport (REQUIRED, an
        engine sport e.g. afl|racing), book?, min_edge_pct?, error_multiple?,
        max_age_minutes?, places (racing only: the book's paid place terms)}.
        channel: a Slack channel id, or "log"."""
        kind = str(args["kind"])
        if kind not in _KINDS:
            raise ValueError(f"kind must be one of {_KINDS}")
        params = dict(args.get("params") or {})
        if kind == "model_value":
            # a sport-less watch raises every cycle forever; a placeless racing
            # watch can only guess the book's paid terms — refuse both at creation
            if not params.get("sport"):
                raise ValueError("model_value needs params.sport (an engine sport, e.g. afl|racing)")
            if str(params["sport"]) == "racing" and not params.get("places"):
                raise ValueError("model_value on racing needs params.places (the book's paid place terms)")
        async with session_factory() as session:
            row = Subscription(
                tenant_id=scope.tenant_id, workspace_id=scope.workspace_id,
                name=str(args["name"]), kind=kind,
                params=params,
                channel=str(args.get("channel", "log")),
            )
            session.add(row)
            await session.flush()
            watch_id = str(row.id)
            await session.commit()
        return {"watch_id": watch_id, "kind": kind, "name": args["name"]}

    async def list_watches(args: dict[str, Any]) -> Any:
        """The workspace's standing watches."""
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(Subscription).where(
                        Subscription.tenant_id == scope.tenant_id,
                        Subscription.workspace_id == scope.workspace_id,
                    )
                )
            ).scalars().all()
        return {"watches": [
            {"watch_id": str(r.id), "name": r.name, "kind": r.kind, "params": r.params,
             "channel": r.channel, "active": r.active,
             "cursor": r.cursor.isoformat() if r.cursor else None}
            for r in rows
        ]}

    async def delete_watch(args: dict[str, Any]) -> Any:
        """{watch_id} → deactivate a watch (history stays)."""
        import uuid as _uuid

        async with session_factory() as session:
            row = await session.get(Subscription, _uuid.UUID(str(args["watch_id"])))
            if row is None or row.tenant_id != scope.tenant_id or row.workspace_id != scope.workspace_id:
                raise ValueError("unknown watch_id for this workspace")
            row.active = False
            await session.commit()
        return {"deactivated": str(args["watch_id"])}

    async def list_alerts(args: dict[str, Any]) -> Any:
        """{limit?} → the workspace's most recent alerts, newest first."""
        limit = min(int(args.get("limit", 20)), 100)
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(Alert)
                    .where(Alert.tenant_id == scope.tenant_id,
                           Alert.workspace_id == scope.workspace_id)
                    .order_by(Alert.created_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
        return {"alerts": [
            {"kind": r.kind, "message": r.message, "pushed": r.pushed,
             "at": r.created_at.isoformat() if r.created_at else None}
            for r in rows
        ]}

    def _tool(name: str, fn: Any, props: dict[str, Any], required: list[str]) -> ToolDef:
        return ToolDef(
            name=name,
            description=(fn.__doc__ or name).strip().splitlines()[0],
            parameters={"type": "object", "properties": props, "required": required},
            execute=fn,
        )

    return [
        _tool("create_watch", create_watch,
              {"name": {"type": "string"}, "kind": {"type": "string", "enum": list(_KINDS)},
               "params": {"type": "object"}, "channel": {"type": "string"}},
              ["name", "kind"]),
        _tool("list_watches", list_watches, {}, []),
        _tool("delete_watch", delete_watch, {"watch_id": {"type": "string"}}, ["watch_id"]),
        _tool("list_alerts", list_alerts, {"limit": {"type": "integer"}}, []),
    ]
