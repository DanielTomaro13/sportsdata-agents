"""Conversation threading (P2 backlog → done): turns persist, context returns.

A conversation is keyed by the channel's external id (the Slack thread key). Each
turn stores the user message + the answer; the next turn gets a compact transcript
of the most recent turns prefixed to its prompt — so "what about away games?" in a
Slack thread means something. DB-less deployments stay stateless (the store is only
built when the DbRecorder is live), matching the degradation contract everywhere
else.
"""

from __future__ import annotations

import datetime as dt
import uuid as _uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import Conversation, Message
from sportsdata_agents.data.repository import TenantScope

MAX_CONTEXT_TURNS = 6  # user+assistant pairs threaded back in
MAX_TURN_CHARS = 600  # per stored message slice quoted into context


class ConversationStore:
    """Persist turns per conversation key; render recent context for the next turn."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], scope: TenantScope) -> None:
        self._sf = session_factory
        self._scope = scope

    async def _conversation_id(self, session: AsyncSession, key: str, *, create: bool) -> _uuid.UUID | None:
        row = (
            (
                await session.execute(
                    select(Conversation).where(
                        Conversation.tenant_id == self._scope.tenant_id,
                        Conversation.workspace_id == self._scope.workspace_id,
                        Conversation.external_id == key,
                    )
                )
            )
            .scalars()
            .first()
        )
        if row is not None:
            return row.id
        if not create:
            return None
        channel = key.split("-", 1)[0] if "-" in key else "gateway"
        row = Conversation(
            tenant_id=self._scope.tenant_id,
            workspace_id=self._scope.workspace_id,
            channel=channel[:32],
            external_id=key,
        )
        session.add(row)
        await session.flush()
        return row.id

    async def context_for(self, key: str) -> str | None:
        """A compact transcript of the most recent turns, oldest first; None = no history."""
        async with self._sf() as session:
            conv_id = await self._conversation_id(session, key, create=False)
            if conv_id is None:
                return None
            rows = (
                (
                    await session.execute(
                        select(Message)
                        .where(Message.conversation_id == conv_id)
                        .order_by(Message.created_at.desc(), Message.id.desc())
                        .limit(MAX_CONTEXT_TURNS * 2)
                    )
                )
                .scalars()
                .all()
            )
        if not rows:
            return None
        lines = [f"{m.role}: {m.content[:MAX_TURN_CHARS]}" for m in reversed(rows)]
        return "\n".join(lines)

    async def append_turn(self, key: str, user_text: str, answer_text: str) -> None:
        async with self._sf() as session:
            conv_id = await self._conversation_id(session, key, create=True)
            assert conv_id is not None
            # Explicit microsecond timestamps: the server default is second-resolution
            # on SQLite and ids are random UUIDs — same-second turns would shuffle.
            now = dt.datetime.now(dt.UTC)
            for offset, (role, content) in enumerate((("user", user_text), ("assistant", answer_text))):
                session.add(
                    Message(
                        tenant_id=self._scope.tenant_id,
                        workspace_id=self._scope.workspace_id,
                        conversation_id=conv_id,
                        role=role,
                        content=content,
                        created_at=now + dt.timedelta(microseconds=offset),
                    )
                )
            await session.commit()


def threaded_prompt(context: str | None, text: str) -> str:
    """The prompt the team actually sees: prior turns (when any) + the new message."""
    if not context:
        return text
    return (
        f"[conversation context — most recent turns]\n{context}\n"
        f"[current message — answer THIS]\n{text}"
    )
