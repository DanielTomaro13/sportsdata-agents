"""M1.2 — Slack adapter logic (offline: fake gateway + fake say/client)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from sportsdata_agents.interfaces.slack.app import (
    _strip_mention,
    ask_gateway,
    format_answer,
    handle_question,
    push_notification,
)

pytestmark = pytest.mark.unit


def test_strip_mention() -> None:
    assert _strip_mention("<@U123ABC> who won?") == "who won?"
    assert _strip_mention("plain question") == "plain question"


def test_format_answer_carries_sources_badge_and_disclaimer() -> None:
    text = format_answer({"answer": "Yankees.", "sources": ["mlb_player"], "verified": True, "cost_usd": 0.01})
    assert "Yankees." in text
    assert "mlb_player" in text
    assert "✅ grounded" in text
    assert "informational only" in text
    unverified = format_answer({"answer": "x", "sources": [], "verified": False, "cost_usd": 0})
    assert "⚠️ unverified" in unverified


async def test_ask_gateway_posts_to_conversation_route(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["json"] = request.content
        return httpx.Response(200, json={"answer": "ok", "sources": [], "verified": True,
                                         "cost_usd": 0.0, "stop_reason": "done", "steps": 1, "tool_calls": 0})

    monkeypatch.setenv("AGENTS_GATEWAY_URL", "http://gw")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    out = await ask_gateway("who won?", thread_key="slack-C1-123.45", client=client)
    assert out["answer"] == "ok"
    assert "/conversations/slack-C1-123.45/message" in seen["url"]
    await client.aclose()


class FakeSay:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def __call__(self, *, text: str, thread_ts: str | None = None) -> None:
        self.messages.append({"text": text, "thread_ts": thread_ts})


async def test_handle_question_acknowledges_then_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_gateway(text: str, *, thread_key: str, client: Any = None) -> dict[str, Any]:
        return {"answer": f"answer to: {text}", "sources": ["s"], "verified": True, "cost_usd": 0.01}

    import sportsdata_agents.interfaces.slack.app as slack_app

    monkeypatch.setattr(slack_app, "ask_gateway", fake_gateway)
    say = FakeSay()
    await handle_question("<@U1> best price?", channel="C1", thread_ts="123.45", say=say)
    assert len(say.messages) == 2
    assert "on it" in say.messages[0]["text"]
    assert "answer to: best price?" in say.messages[1]["text"]
    assert all(m["thread_ts"] == "123.45" for m in say.messages)  # threaded replies


async def test_handle_question_gateway_down_is_graceful(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(*a: Any, **kw: Any) -> dict[str, Any]:
        raise httpx.ConnectError("refused")

    import sportsdata_agents.interfaces.slack.app as slack_app

    monkeypatch.setattr(slack_app, "ask_gateway", broken)
    say = FakeSay()
    await handle_question("q", channel="C1", thread_ts="1.2", say=say)
    assert "couldn't reach the team" in say.messages[-1]["text"]


class FakeWebClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def chat_postMessage(self, *, channel: str, text: str) -> None:
        self.posts.append({"channel": channel, "text": text})


async def test_push_notification_delivers_and_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeWebClient()
    assert await push_notification("🔔 CLV report ready", channel="#alerts", client=client) is True
    assert client.posts == [{"channel": "#alerts", "text": "🔔 CLV report ready"}]
    # no channel configured → dropped, not crashed
    monkeypatch.delenv("SLACK_ALERT_CHANNEL", raising=False)
    assert await push_notification("x", client=client) is False
