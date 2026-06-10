"""Slack workspace-admin tools (ops plane): channels, membership, topics, canvases.

Session-bound like tracking tools — built per session with a Slack client when
``SLACK_BOT_TOKEN`` is configured. Used by the ``slack_manager`` agent to keep the
workspace tidy: right channels, right names, right members, right routing.

Safety: archive/rename are destructive-ish — the agent's spec instructs it to act
only on explicit instruction; every mutation returns what changed so the user sees
it in the thread. Canvas creation degrades clearly on plans without the API.
Routing convention: alert routes live in agent memory as ``route:<kind>`` →
``#channel`` (set via `remember`, read via `recall`/`resolve_route`).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from sportsdata_agents.agents.harness import ToolDef

logger = logging.getLogger(__name__)

SLACK_ADMIN_TOOL_NAMES = {
    "list_channels",
    "create_channel",
    "rename_channel",
    "archive_channel",
    "set_channel_topic",
    "invite_to_channel",
    "channel_members",
    "post_to_channel",
    "create_canvas",
}


def _client(client: Any = None) -> Any:
    if client is not None:
        return client
    from slack_sdk.web.async_client import AsyncWebClient

    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN not configured")
    return AsyncWebClient(token=token)


def slack_admin_tools(client: Any = None) -> list[ToolDef]:
    c = _client(client)

    async def list_channels(args: dict[str, Any]) -> Any:
        """All public channels with id/name/topic/membership."""
        out = await c.conversations_list(types="public_channel", limit=int(args.get("limit", 100)))
        return {
            "channels": [
                {
                    "id": ch["id"],
                    "name": ch["name"],
                    "topic": (ch.get("topic") or {}).get("value", ""),
                    "is_member": ch.get("is_member", False),
                    "num_members": ch.get("num_members"),
                }
                for ch in out["channels"]
            ]
        }

    async def create_channel(args: dict[str, Any]) -> Any:
        """{name} → create a public channel (lowercase, hyphens) and join it."""
        out = await c.conversations_create(name=str(args["name"]).lower())
        return {"created": out["channel"]["name"], "id": out["channel"]["id"]}

    async def rename_channel(args: dict[str, Any]) -> Any:
        """{channel_id, new_name} — only on explicit user instruction."""
        out = await c.conversations_rename(channel=str(args["channel_id"]), name=str(args["new_name"]).lower())
        return {"renamed_to": out["channel"]["name"], "id": out["channel"]["id"]}

    async def archive_channel(args: dict[str, Any]) -> Any:
        """{channel_id} — only on explicit user instruction."""
        await c.conversations_archive(channel=str(args["channel_id"]))
        return {"archived": args["channel_id"]}

    async def set_channel_topic(args: dict[str, Any]) -> Any:
        """{channel_id, topic} → set the channel topic (joins first if needed)."""
        import contextlib

        with contextlib.suppress(Exception):  # already a member / private — topic call decides
            await c.conversations_join(channel=str(args["channel_id"]))
        await c.conversations_setTopic(channel=str(args["channel_id"]), topic=str(args["topic"]))
        return {"channel": args["channel_id"], "topic": args["topic"]}

    async def invite_to_channel(args: dict[str, Any]) -> Any:
        """{channel_id, user_ids: [..]} → invite users."""
        users = ",".join(str(u) for u in args["user_ids"])
        await c.conversations_invite(channel=str(args["channel_id"]), users=users)
        return {"channel": args["channel_id"], "invited": args["user_ids"]}

    async def channel_members(args: dict[str, Any]) -> Any:
        """{channel_id} → member user ids."""
        out = await c.conversations_members(channel=str(args["channel_id"]))
        return {"channel": args["channel_id"], "members": out["members"]}

    async def post_to_channel(args: dict[str, Any]) -> Any:
        """{channel_id, text} → post (e.g. routing announcements, audit summaries)."""
        out = await c.chat_postMessage(channel=str(args["channel_id"]), text=str(args["text"]))
        return {"posted": out["ts"], "channel": args["channel_id"]}

    async def create_canvas(args: dict[str, Any]) -> Any:
        """{channel_id, title, markdown} → channel canvas; degrades clearly when the
        workspace plan lacks the canvases API."""
        try:
            out = await c.conversations_canvases_create(
                channel_id=str(args["channel_id"]),
                document_content={"type": "markdown", "markdown": str(args["markdown"])},
            )
            return {"canvas_id": out.get("canvas_id"), "channel": args["channel_id"]}
        except Exception as e:
            return {
                "error": f"canvas creation unavailable: {type(e).__name__}: {str(e)[:120]} — "
                f"likely a free-plan limitation; posting as a pinned message is the fallback"
            }

    def _tool(name: str, fn: Any, props: dict[str, Any], required: list[str]) -> ToolDef:
        return ToolDef(
            name=name,
            description=(fn.__doc__ or name).strip().splitlines()[0],
            parameters={"type": "object", "properties": props, "required": required},
            execute=fn,
        )

    return [
        _tool("list_channels", list_channels, {"limit": {"type": "integer"}}, []),
        _tool("create_channel", create_channel, {"name": {"type": "string"}}, ["name"]),
        _tool(
            "rename_channel",
            rename_channel,
            {"channel_id": {"type": "string"}, "new_name": {"type": "string"}},
            ["channel_id", "new_name"],
        ),
        _tool("archive_channel", archive_channel, {"channel_id": {"type": "string"}}, ["channel_id"]),
        _tool(
            "set_channel_topic",
            set_channel_topic,
            {"channel_id": {"type": "string"}, "topic": {"type": "string"}},
            ["channel_id", "topic"],
        ),
        _tool(
            "invite_to_channel",
            invite_to_channel,
            {"channel_id": {"type": "string"}, "user_ids": {"type": "array", "items": {"type": "string"}}},
            ["channel_id", "user_ids"],
        ),
        _tool("channel_members", channel_members, {"channel_id": {"type": "string"}}, ["channel_id"]),
        _tool(
            "post_to_channel",
            post_to_channel,
            {"channel_id": {"type": "string"}, "text": {"type": "string"}},
            ["channel_id", "text"],
        ),
        _tool(
            "create_canvas",
            create_canvas,
            {
                "channel_id": {"type": "string"},
                "title": {"type": "string"},
                "markdown": {"type": "string"},
            },
            ["channel_id", "markdown"],
        ),
    ]
