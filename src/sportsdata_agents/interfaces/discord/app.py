"""Discord adapter (M3.3): mentions/DMs → gateway, channel replies.

Same thin shape as the Slack adapter — Discord is just another caller of the
gateway HTTP surface: an @mention or DM posts to
``/conversations/{channel}/message`` (the Discord channel IS the conversation
key) and replies in-channel. Env:

  DISCORD_BOT_TOKEN       (the bot's token; needs the message-content intent)
  AGENTS_GATEWAY_URL      (default http://127.0.0.1:8400)

The discord.py dependency is optional: ``pip install 'sportsdata-agents[discord]'``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from sportsdata_agents.interfaces.slack.app import format_answer, gateway_url

logger = logging.getLogger(__name__)

MAX_DISCORD_LEN = 1900  # under the 2000-char message cap, leaving room for ellipsis


def strip_bot_mention(text: str, bot_id: int | None) -> str:
    """Remove a leading <@id>/<@!id> mention so the prompt is just the question."""
    text = text.strip()
    for prefix in (f"<@{bot_id}>", f"<@!{bot_id}>"):
        if bot_id is not None and text.startswith(prefix):
            text = text[len(prefix):]
    return text.strip()


def clip(message: str) -> str:
    return message if len(message) <= MAX_DISCORD_LEN else message[: MAX_DISCORD_LEN] + "…"


async def ask_gateway(text: str, *, channel_key: str) -> dict[str, Any]:
    """POST the question to the gateway, keyed by the Discord channel."""
    async with httpx.AsyncClient(timeout=300) as client:
        response = await client.post(
            f"{gateway_url()}/conversations/discord-{channel_key}/message",
            json={"text": text},
        )
        response.raise_for_status()
        return response.json()


async def handle_message(content: str, *, channel_key: str, bot_id: int | None) -> str | None:
    """The routing core (unit-testable without discord.py): a mention/DM's text →
    the formatted reply, or None when there's nothing to answer."""
    prompt = strip_bot_mention(content, bot_id)
    if not prompt:
        return None
    try:
        payload = await ask_gateway(prompt, channel_key=channel_key)
    except httpx.HTTPError as e:
        logger.warning("gateway error: %s", e)
        return f"⚠️ the agent gateway is unreachable ({type(e).__name__}) — is `agents serve` running?"
    return clip(format_answer(payload))


def serve_bot() -> None:
    """Run the Discord bot (blocking). Needs DISCORD_BOT_TOKEN + a running gateway."""
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is not set")
    try:
        import discord
    except ImportError as e:
        raise SystemExit(
            "discord.py is not installed — pip install 'sportsdata-agents[discord]'"
        ) from e

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        logger.info("discord adapter ready as %s", client.user)

    @client.event
    async def on_message(message: Any) -> None:
        if client.user is None or message.author == client.user:
            return
        is_dm = getattr(message.channel, "type", None) and str(message.channel.type) == "private"
        mentioned = client.user in getattr(message, "mentions", [])
        if not (is_dm or mentioned):
            return
        reply = await handle_message(
            message.content, channel_key=str(message.channel.id), bot_id=client.user.id
        )
        if reply:
            await message.channel.send(reply)

    client.run(token)
