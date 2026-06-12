"""Notification fan-out: Slack/Discord routing, dialect translation, broadcast."""

from __future__ import annotations

from typing import Any

import pytest

from sportsdata_agents.observability import notify

pytestmark = pytest.mark.unit


def test_slack_to_discord_translates_bold_only() -> None:
    assert notify.slack_to_discord("*ARB 2.4%* on h2h") == "**ARB 2.4%** on h2h"
    # already-discord bold and emoji shortcodes pass through untouched
    assert notify.slack_to_discord("**keep** :fire: a*b") == "**keep** :fire: a*b"


def test_discord_webhook_env_selection(monkeypatch: Any) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://d/default")
    monkeypatch.setenv("MY_ARBS_HOOK", "https://d/arbs")
    assert notify.discord_webhook_for("discord") == "https://d/default"
    assert notify.discord_webhook_for("discord:MY_ARBS_HOOK") == "https://d/arbs"
    monkeypatch.delenv("DISCORD_WEBHOOK_URL")
    assert notify.discord_webhook_for("discord") is None


async def test_push_to_channel_routes_by_platform(monkeypatch: Any) -> None:
    sent: list[tuple[str, str]] = []

    async def fake_slack(channel: str, text: str) -> bool:
        sent.append(("slack", channel))
        return True

    async def fake_discord(url: str, text: str) -> bool:
        sent.append(("discord", url))
        return True

    monkeypatch.setattr(notify, "post_slack", fake_slack)
    monkeypatch.setattr(notify, "post_discord", fake_discord)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://d/default")

    assert await notify.push_to_channel("log", "x") is False  # log only
    assert await notify.push_to_channel("C0B9XH11476", "x") is True
    assert await notify.push_to_channel("discord", "x") is True
    assert sent == [("slack", "C0B9XH11476"), ("discord", "https://d/default")]


async def test_operator_broadcast_hits_every_configured_target(monkeypatch: Any) -> None:
    sent: list[str] = []

    async def fake_slack(channel: str, text: str) -> bool:
        sent.append(f"slack:{channel}")
        return True

    async def fake_discord(url: str, text: str) -> bool:
        sent.append(f"discord:{url}")
        return True

    monkeypatch.setattr(notify, "post_slack", fake_slack)
    monkeypatch.setattr(notify, "post_discord", fake_discord)

    # nothing configured: no targets, logged only
    for var in ("OPS_SLACK_CHANNEL", "OPS_DISCORD_WEBHOOK", "SLACK_BOT_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    assert await notify.operator_broadcast("report") == {}

    # both configured: BOTH receive it — the platforms are equals
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("OPS_SLACK_CHANNEL", "C123")
    monkeypatch.setenv("OPS_DISCORD_WEBHOOK", "https://d/ops")
    results = await notify.operator_broadcast("report")
    assert results == {"slack": True, "discord": True}
    assert sent == ["slack:C123", "discord:https://d/ops"]


async def test_alert_subscription_can_target_discord(monkeypatch: Any) -> None:
    """The monitor's pusher routes a discord-channel subscription to the webhook."""
    from sportsdata_agents.data.models import Subscription
    from sportsdata_agents.operations.monitoring import slack_pusher

    hits: list[str] = []

    async def fake_discord(url: str, text: str) -> bool:
        hits.append(url)
        return True

    monkeypatch.setattr(notify, "post_discord", fake_discord)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://d/alerts")
    sub = Subscription(tenant_id="t", workspace_id="w", name="arbs", kind="arb",
                       params={}, channel="discord")
    assert await slack_pusher(sub, ":money_with_wings: ARB 2.4%") is True
    assert hits == ["https://d/alerts"]
