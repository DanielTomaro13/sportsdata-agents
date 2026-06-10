"""Slack adapter (M1.2, D4): events → gateway, threaded replies, push notifications.

Thin by design — Slack is just another caller of the gateway HTTP surface:
- an @mention or DM posts to ``/conversations/{thread}/message`` (the Slack thread
  IS the conversation key) and replies in-thread;
- ``/ask <question>`` slash command does the same from anywhere;
- :func:`push_notification` lets reporting agents deliver alerts to a channel.

Runs in **Socket Mode** (no public URL needed locally). Env:
  SLACK_BOT_TOKEN  (xoxb-…)   SLACK_APP_TOKEN  (xapp-…, Socket Mode)
  AGENTS_GATEWAY_URL           (default http://127.0.0.1:8400)
  SLACK_ALERT_CHANNEL          (optional default channel for push alerts)
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GATEWAY_DEFAULT = "http://127.0.0.1:8400"
THINKING = "🤔 on it — asking the team…"


def gateway_url() -> str:
    return os.environ.get("AGENTS_GATEWAY_URL", GATEWAY_DEFAULT)


def _strip_mention(text: str) -> str:
    """Remove the leading <@BOTID> from an app_mention's text."""
    text = text.strip()
    if text.startswith("<@"):
        end = text.find(">")
        if end != -1:
            text = text[end + 1 :]
    return text.strip()


def format_answer(payload: dict[str, Any]) -> str:
    """Gateway MessageOut → Slack mrkdwn (sources + the §14 advisory line)."""
    from sportsdata_agents.agents.grounding import ADVISORY_DISCLAIMER

    answer = payload.get("answer", "(no answer)")
    lines = [answer]
    if payload.get("sources"):
        lines.append(f"_sources: {', '.join(payload['sources'])}_")
    verified = payload.get("verified")
    badge = "✅ grounded" if verified else ("⚠️ unverified" if verified is False else "")
    footer = " · ".join(x for x in (badge, f"${payload.get('cost_usd', 0):.4f}", ADVISORY_DISCLAIMER) if x)
    lines.append(f"_{footer}_")
    return "\n\n".join(lines)


async def ask_gateway(text: str, *, thread_key: str, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """POST the question to the gateway, keyed by the Slack thread."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=600.0)
    try:
        r = await client.post(
            f"{gateway_url()}/conversations/{thread_key}/message",
            json={"text": text},
        )
        r.raise_for_status()
        return r.json()
    finally:
        if own:
            await client.aclose()


async def handle_question(text: str, *, channel: str, thread_ts: str, say: Any) -> None:
    """Shared flow for mentions, DMs and /ask: acknowledge → run → threaded answer.
    An empty ``thread_ts`` (slash commands) posts unthreaded — Slack rejects ``""``."""
    threaded: dict[str, Any] = {"thread_ts": thread_ts} if thread_ts else {}
    question = _strip_mention(text)
    if not question:
        await say(text="Ask me something — e.g. `which team does Aaron Judge play for?`", **threaded)
        return
    await say(text=THINKING, **threaded)
    try:
        payload = await ask_gateway(question, thread_key=f"slack-{channel}-{thread_ts or 'direct'}")
        await say(text=format_answer(payload), **threaded)
    except Exception as e:
        logger.warning("gateway call failed: %s: %s", type(e).__name__, e)
        await say(text=f"⚠️ couldn't reach the team: {type(e).__name__}", **threaded)


async def push_notification(text: str, *, channel: str | None = None, client: Any = None) -> bool:
    """Deliver a push alert (used by reporting agents). Returns delivery success."""
    channel = channel or os.environ.get("SLACK_ALERT_CHANNEL")
    if not channel:
        logger.warning("push_notification dropped: no channel (set SLACK_ALERT_CHANNEL)")
        return False
    if client is None:
        from slack_sdk.web.async_client import AsyncWebClient

        token = os.environ.get("SLACK_BOT_TOKEN")
        if not token:
            logger.warning("push_notification dropped: SLACK_BOT_TOKEN not set")
            return False
        client = AsyncWebClient(token=token)
    await client.chat_postMessage(channel=channel, text=text)
    return True


def is_user_dm(event: dict[str, Any]) -> bool:
    """Only fresh human DMs get an answer: skip bot echoes and subtype events
    (message_changed/_deleted would otherwise trigger a reply on every edit)."""
    return event.get("channel_type") == "im" and not event.get("bot_id") and not event.get("subtype")


def build_app() -> Any:
    """The Bolt AsyncApp with handlers bound (requires Slack tokens)."""
    from slack_bolt.async_app import AsyncApp

    app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])

    @app.event("app_mention")
    async def on_mention(event: dict[str, Any], say: Any) -> None:
        await handle_question(
            event.get("text", ""),
            channel=event["channel"],
            thread_ts=event.get("thread_ts") or event["ts"],
            say=say,
        )

    @app.event("message")
    async def on_dm(event: dict[str, Any], say: Any) -> None:
        if not is_user_dm(event):
            return
        await handle_question(
            event.get("text", ""),
            channel=event["channel"],
            thread_ts=event.get("thread_ts") or event["ts"],
            say=say,
        )

    @app.command("/ask")
    async def on_ask(ack: Any, command: dict[str, Any], say: Any) -> None:
        await ack()
        await handle_question(
            command.get("text", ""),
            channel=command["channel_id"],
            thread_ts=command.get("thread_ts") or "",
            say=say,
        )

    return app


def serve_socket_mode() -> None:
    """Run the adapter (Socket Mode — no public URL needed)."""
    import asyncio

    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    async def main() -> None:
        app = build_app()
        handler = AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
        await handler.start_async()

    asyncio.run(main())
