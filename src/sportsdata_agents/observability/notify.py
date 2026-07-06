"""Outbound notifications: Slack, Discord, and ntfy as EQUALS, in one place.

Every push surface routes through here — monitor alerts, ops reports,
escalations — so adding a platform (or fixing formatting) is one edit:

- **Slack** posts via ``chat.postMessage`` (``SLACK_BOT_TOKEN`` + a channel id).
- **Discord** posts via webhooks — no bot needed: create a channel webhook in
  Discord (Channel settings → Integrations → Webhooks) and set the URL in
  ``DISCORD_WEBHOOK_URL`` (alerts) / ``OPS_DISCORD_WEBHOOK`` (ops reports).
  The chat adapter (``agents discord``, ``DISCORD_BOT_TOKEN``) is separate —
  webhooks deliver even when the bot is not running.
- **ntfy** posts a plain HTTP publish to a topic URL — native phone push via
  the ntfy app, no account. Set the FULL topic URL in ``NTFY_TOPIC_URL``
  (alerts) / ``OPS_NTFY_URL`` (ops reports); the server rides in the URL, so
  a self-hosted ntfy is the same config. SECURITY: on the public ntfy.sh
  server anyone who knows the topic name can read it — use a long random
  topic (``sd-`` + 24+ random chars) or self-host; alert text is your edge.

Alert subscriptions pick their platform with the ``channel`` field:
a Slack channel id ("C…") posts to Slack; ``"discord"`` posts to the default
webhook; ``"discord:MY_ENV_VAR"`` posts to the webhook named by that env var;
``"ntfy"`` / ``"ntfy:MY_ENV_VAR"`` publish to a topic URL the same way;
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
NTFY_LIMIT = 4000  # ntfy default max body is 4KB
_BOLD = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_EMOJI = re.compile(r":[a-z0-9_+-]+:")


def slack_to_discord(text: str) -> str:
    """Slack mrkdwn → Discord markdown: single-asterisk bold becomes double."""
    return _BOLD.sub(r"**\1**", text)


def slack_to_plain(text: str) -> str:
    """Slack mrkdwn → plain text for ntfy: bold markers and emoji shortcodes
    render literally there, so both are stripped."""
    return _EMOJI.sub("", _BOLD.sub(r"\1", text)).strip()


def _ascii_header(text: str) -> str:
    """Fold a title to ASCII for an HTTP header (latin-1 only). Accents map to
    their base letter (José→Jose), Unicode dashes to '-', anything else drops —
    an un-encodable char here would sink the whole ntfy push inside httpx."""
    import unicodedata

    # em dash / en dash / curly apostrophe (escaped so linters don't trip)
    folded = (text.replace("\u2014", "-").replace("\u2013", "-")
              .replace("\u2019", "'"))
    return unicodedata.normalize("NFKD", folded).encode("ascii", "ignore").decode("ascii")


def discord_webhook_for(channel: str) -> str | None:
    """The webhook URL a subscription channel names; None = not configured."""
    _, _, env_name = channel.partition(":")
    return os.environ.get(env_name.strip() or "DISCORD_WEBHOOK_URL")


def ntfy_url_for(channel: str) -> str | None:
    """The ntfy topic URL a subscription channel names; None = not configured."""
    _, _, env_name = channel.partition(":")
    return os.environ.get(env_name.strip() or "NTFY_TOPIC_URL")


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


# alert-kind styling: the leading emoji shortcode picks the embed's colour
# strip and its unicode stand-in (Discord embeds do NOT render :shortcodes:)
_EMBED_STYLE: dict[str, tuple[str, int]] = {
    "racehorse": ("\U0001F3C7", 0x2ECC71),          # racing — green
    "fire": ("\U0001F525", 0xE67E22),               # steam — orange
    "scales": ("\u2696\uFE0F", 0x3498DB),          # exchange premium — blue
    "dart": ("\U0001F3AF", 0x9B59B6),               # stat/prop value — purple
    "chart_with_upwards_trend": ("\U0001F4C8", 0x1ABC9C),  # line move — teal
    "moneybag": ("\U0001F4B0", 0xF1C40F),           # arb — gold
    "money_with_wings": ("\U0001F4B8", 0xF1C40F),
    "crystal_ball": ("\U0001F52E", 0x9B59B6),       # prediction markets
    "rotating_light": ("\U0001F6A8", 0xE74C3C),     # incidents — red
    "white_check_mark": ("\u2705", 0x2ECC71),
    "warning": ("\u26A0\uFE0F", 0xF39C12),
    "floppy_disk": ("\U0001F4BE", 0x95A5A6),
    "bar_chart": ("\U0001F4CA", 0x3498DB),
    "bell": ("\U0001F514", 0x95A5A6),
}
_DEFAULT_EMBED_COLOR = 0x95A5A6


def _emojify(text: str) -> str:
    """Shortcodes → unicode (embeds don't render shortcodes); unknown ones drop."""
    return _EMOJI.sub(lambda m: _EMBED_STYLE.get(m.group(0).strip(":"), ("", 0))[0], text)


def discord_embed(text: str) -> dict:
    """A colour-striped embed from an alert message: first line is the title,
    the rest the body; the leading emoji picks the colour. An "across books:"
    line becomes a FIELD GRID — one inline field per book, an actual table."""
    first_code = _EMOJI.match(text.strip())
    color = _DEFAULT_EMBED_COLOR
    if first_code:
        color = _EMBED_STYLE.get(first_code.group(0).strip(":"), ("", _DEFAULT_EMBED_COLOR))[1]
    title, _, body = text.strip().partition("\n")
    fields: list[dict] = []
    kept_lines: list[str] = []
    for line in body.splitlines():
        if line.startswith("across books:"):
            for pair in line.removeprefix("across books:").split("·"):
                parts = pair.strip().rsplit(" ", 1)
                if len(parts) == 2:
                    fields.append({"name": parts[0][:64], "value": f"**{parts[1]}**",
                                   "inline": True})
            continue
        kept_lines.append(line)
    embed = {
        "title": _emojify(slack_to_discord(title)).strip()[:256],
        "description": _emojify(slack_to_discord("\n".join(kept_lines))).strip()[:4000],
        "color": color,
    }
    if fields:
        embed["fields"] = fields[:12]
    return embed


async def post_discord(webhook_url: str, text: str) -> bool:
    if not webhook_url:
        logger.info("discord (unconfigured): %s", text)
        return False
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(webhook_url, json={"embeds": [discord_embed(text)]})
        if response.status_code not in (200, 204):
            # a malformed embed must never lose the alert — plain text fallback
            response = await client.post(
                webhook_url, json={"content": slack_to_discord(text)[:DISCORD_LIMIT]})
    ok = response.status_code in (200, 204)
    if not ok:
        logger.warning("discord push failed: %s %s", response.status_code, response.text[:200])
    return ok


async def post_ntfy(topic_url: str, text: str, *, priority: str | None = None) -> bool:
    """Publish to an ntfy topic — the body is the notification. ``priority``
    is ntfy's scale ("min".."max"/"urgent"); None leaves the server default."""
    if not topic_url:
        logger.info("ntfy (unconfigured): %s", text)
        return False
    import httpx

    plain = slack_to_plain(text)
    # first line becomes the notification title; the rest the body
    title, _, body = plain.partition("\n")
    headers = {"Title": _ascii_header(title[:250])} if body else {}
    lead = _EMOJI.match(text.strip())
    if lead:
        headers["Tags"] = lead.group(0).strip(":")  # ntfy renders the emoji
    if priority:
        headers["Priority"] = priority
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(topic_url, content=(body or plain)[:NTFY_LIMIT],
                                     headers=headers)
    ok = response.status_code == 200
    if not ok:
        logger.warning("ntfy push failed: %s %s", response.status_code, response.text[:200])
    return ok


async def push_to_channel(channel: str, text: str) -> bool:
    """The alert router: 'log' logs, 'discord[:ENV]' hits a webhook,
    'ntfy[:ENV]' publishes to a topic, anything else is a Slack channel id."""
    if channel in ("", "log"):
        logger.info("alert (log): %s", text)
        return False
    if channel == "discord" or channel.startswith("discord:"):
        return await post_discord(discord_webhook_for(channel) or "", text)
    if channel == "ntfy" or channel.startswith("ntfy:"):
        return await post_ntfy(ntfy_url_for(channel) or "", text)
    return await post_slack(channel, text)


async def operator_broadcast(text: str) -> dict[str, bool]:
    """Ops reports/escalations go to EVERY configured operator target —
    Slack (OPS_SLACK_CHANNEL), Discord (OPS_DISCORD_WEBHOOK) and ntfy
    (OPS_NTFY_URL) are equals."""
    results: dict[str, bool] = {}
    slack_channel = os.environ.get("OPS_SLACK_CHANNEL")
    if slack_channel and os.environ.get("SLACK_BOT_TOKEN"):
        results["slack"] = await post_slack(slack_channel, text)
    discord_webhook = os.environ.get("OPS_DISCORD_WEBHOOK")
    if discord_webhook:
        results["discord"] = await post_discord(discord_webhook, text)
    ntfy_url = os.environ.get("OPS_NTFY_URL")
    if ntfy_url:
        results["ntfy"] = await post_ntfy(ntfy_url, text)
    if not results:
        logger.info("operator broadcast (no targets configured): %s", text)
    return results
