"""Outbound notifications: Slack and Discord as EQUALS, in one place.

Every push surface routes through here — monitor alerts, ops reports,
escalations — so adding a platform (or fixing formatting) is one edit:

- **Slack** posts via ``chat.postMessage`` (``SLACK_BOT_TOKEN`` + a channel id).
- **Discord** posts via webhooks — no bot needed: create a channel webhook in
  Discord (Channel settings → Integrations → Webhooks) and set the URL in
  ``DISCORD_WEBHOOK_URL`` (alerts) / ``OPS_DISCORD_WEBHOOK`` (ops reports).
  The chat adapter (``agents discord``, ``DISCORD_BOT_TOKEN``) is separate —
  webhooks deliver even when the bot is not running.

Alert subscriptions pick their platform with the ``channel`` field:
a Slack channel id ("C…") posts to Slack; ``"discord"`` posts to the default
webhook; ``"discord:MY_ENV_VAR"`` posts to the webhook named by that env var;
``"log"`` only logs. Operator surfaces broadcast to EVERY configured target.

Messages are written in Slack mrkdwn; ``_slack_to_discord`` converts the two
dialect differences that matter (*bold* → **bold**; emoji shortcodes render on
both platforms as-is).
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

DISCORD_LIMIT = 2000  # hard message cap (Slack's 40k never binds first)
_BOLD = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")


def slack_to_discord(text: str) -> str:
    """Slack mrkdwn → Discord markdown: single-asterisk bold becomes double."""
    return _BOLD.sub(r"**\1**", text)


def discord_webhook_for(channel: str) -> str | None:
    """The webhook URL a subscription channel names; None = not configured."""
    _, _, env_name = channel.partition(":")
    return os.environ.get(env_name.strip() or "DISCORD_WEBHOOK_URL")


async def post_slack(channel: str, text: str) -> bool:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token or not channel:
        logger.info("slack (unconfigured): %s", text)
        return False
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "text": text},
        )
    ok = bool(response.json().get("ok"))
    if not ok:
        logger.warning("slack push failed: %s", response.text[:200])
    return ok


async def post_discord(webhook_url: str, text: str) -> bool:
    if not webhook_url:
        logger.info("discord (unconfigured): %s", text)
        return False
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            webhook_url, json={"content": slack_to_discord(text)[:DISCORD_LIMIT]}
        )
    ok = response.status_code in (200, 204)
    if not ok:
        logger.warning("discord push failed: %s %s", response.status_code, response.text[:200])
    return ok


async def push_to_channel(channel: str, text: str) -> bool:
    """The alert router: 'log' logs, 'discord[:ENV]' hits a webhook, anything
    else is a Slack channel id."""
    if channel in ("", "log"):
        logger.info("alert (log): %s", text)
        return False
    if channel == "discord" or channel.startswith("discord:"):
        return await post_discord(discord_webhook_for(channel) or "", text)
    return await post_slack(channel, text)


async def operator_broadcast(text: str) -> dict[str, bool]:
    """Ops reports/escalations go to EVERY configured operator target —
    Slack (OPS_SLACK_CHANNEL) and Discord (OPS_DISCORD_WEBHOOK) are equals."""
    results: dict[str, bool] = {}
    slack_channel = os.environ.get("OPS_SLACK_CHANNEL")
    if slack_channel and os.environ.get("SLACK_BOT_TOKEN"):
        results["slack"] = await post_slack(slack_channel, text)
    discord_webhook = os.environ.get("OPS_DISCORD_WEBHOOK")
    if discord_webhook:
        results["discord"] = await post_discord(discord_webhook, text)
    if not results:
        logger.info("operator broadcast (no targets configured): %s", text)
    return results
