"""P2 backlog — conversation threading: turns persist, context returns, scoped."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.repository import TenantScope
from sportsdata_agents.gateway.conversations import ConversationStore, threaded_prompt

pytestmark = pytest.mark.integration

SCOPE = TenantScope("t", "w")


async def test_turns_persist_and_thread_back(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    store = ConversationStore(db_sessionmaker, SCOPE)
    key = "slack-C1-123.45"
    assert await store.context_for(key) is None  # fresh thread → stateless

    await store.append_turn(key, "how did the Knicks do at home?", "They are 8-2 at home.")
    context = await store.context_for(key)
    assert context is not None
    assert "user: how did the Knicks do at home?" in context
    assert "assistant: They are 8-2 at home." in context

    prompt = threaded_prompt(context, "and away?")
    assert prompt.index("8-2 at home") < prompt.index("[current message")  # context precedes
    assert prompt.rstrip().endswith("and away?")

    # a SECOND turn appends; order is oldest → newest
    await store.append_turn(key, "and away?", "3-7 on the road.")
    context2 = await store.context_for(key)
    assert context2 is not None
    assert context2.index("at home?") < context2.index("on the road.")


async def test_context_trims_to_recent_turns(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    from sportsdata_agents.gateway.conversations import MAX_CONTEXT_TURNS

    store = ConversationStore(db_sessionmaker, SCOPE)
    for i in range(MAX_CONTEXT_TURNS + 3):
        await store.append_turn("k", f"q{i}", f"a{i}")
    context = await store.context_for("k")
    assert context is not None
    assert f"a{MAX_CONTEXT_TURNS + 2}" in context  # newest kept
    assert "q0" not in context  # oldest trimmed


async def test_conversations_are_tenant_scoped(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    mine = ConversationStore(db_sessionmaker, SCOPE)
    other = ConversationStore(db_sessionmaker, TenantScope("other", "o"))
    await mine.append_turn("shared-key", "secret question", "secret answer")
    assert await other.context_for("shared-key") is None


def test_threaded_prompt_stateless_without_context() -> None:
    assert threaded_prompt(None, "hi") == "hi"
    assert threaded_prompt("", "hi") == "hi"
