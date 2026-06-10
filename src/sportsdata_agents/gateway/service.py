"""Channel-agnostic team session (the seam Slack/web reuse later, M1.1/M1.2).

``TeamSession`` owns everything a channel needs: specs, the model gateway, the MCP
session pool, optional audit recording, and the opened team (or a single agent).
Channels stay thin: open a session, feed it prompts, render ``RunResult``s.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from contextlib import AsyncExitStack
from types import TracebackType
from typing import Any, Self

from sportsdata_agents.agents.harness import CompletionProvider, RunResult
from sportsdata_agents.agents.loader import load_builtin_specs
from sportsdata_agents.agents.runtime import AgentRuntime, open_team
from sportsdata_agents.agents.spec import AgentSpec
from sportsdata_agents.config import Settings, get_settings
from sportsdata_agents.data.repository import TenantScope
from sportsdata_agents.mcp.pool import MCPSessionPool
from sportsdata_agents.models.gateway import ModelGateway
from sportsdata_agents.observability.recorder import DbRecorder, RunRecorder
from sportsdata_agents.workspace import Workspace, default_workspace

logger = logging.getLogger(__name__)


# Provider detection (BYO-LLM, §8.1): the first configured key wins. None = the
# policy's default tiers already work (Anthropic primary). A single OpenRouter /
# Gemini / Groq key pins every tier to that provider's model.
_PROVIDER_KEYS: list[tuple[str, str | None]] = [
    ("ANTHROPIC_API_KEY", None),
    ("OPENROUTER_API_KEY", "openrouter/openai/gpt-4o-mini"),
    ("GEMINI_API_KEY", "gemini/gemini-2.0-flash"),
    ("GROQ_API_KEY", "groq/llama-3.3-70b-versatile"),
    ("OPENAI_API_KEY", "openai/gpt-4o-mini"),
]


def detect_tier_overrides() -> dict[str, str]:
    """Workspace ``model_tiers`` overrides for whichever model key is configured."""
    import os

    for env_name, model in _PROVIDER_KEYS:
        if os.environ.get(env_name):
            if model is None:
                return {}
            return {"fast": model, "balanced": model, "strong": model}
    return {}


def has_model_key() -> bool:
    """True when any supported model-provider key is configured (BYO-LLM, §8.1)."""
    import os

    return any(os.environ.get(env_name) for env_name, _ in _PROVIDER_KEYS)


async def try_db_recorder(settings: Settings, scope: TenantScope) -> DbRecorder | None:
    """A DbRecorder when the database actually accepts a connection; None (with ONE
    warning) otherwise — the CLI must work without Postgres running, just without
    audit rows. The probe matters: the sessionmaker is lazy and 'succeeds' even when
    the DB is down, which would otherwise surface as guarded-hook warning spam on
    every run instead of a single clear notice."""
    try:
        from sportsdata_agents.data.db import get_engine, get_sessionmaker

        engine = get_engine()
        async with engine.connect():
            pass
        return DbRecorder(get_sessionmaker(), scope)
    except Exception as e:
        logger.warning("audit disabled — database unavailable (%s: %s)", type(e).__name__, e)
        return None


class TeamSession:
    """One opened team (or single agent), ready to answer prompts."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        workspace: Workspace | None = None,
        specs: dict[str, AgentSpec] | None = None,
        provider: CompletionProvider | None = None,
        recorder: RunRecorder | None = None,
        agent_id: str | None = None,
        root_id: str = "orchestrator",
        mcp_command: Sequence[str] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.workspace = workspace or default_workspace(self.settings)
        self.specs = specs or load_builtin_specs()
        self.recorder = recorder
        usage_sink = getattr(recorder, "usage_sink", None) if recorder is not None else None
        self.provider = provider or ModelGateway(usage_sink=usage_sink)
        self.agent_id = agent_id
        self.root_id = root_id
        self._mcp_command = list(mcp_command) if mcp_command else list(self.settings.mcp_command)
        self._stack: AsyncExitStack | None = None
        self._runtime: AgentRuntime | None = None

    @property
    def agent_name(self) -> str:
        return self.agent_id or self.root_id

    async def __aenter__(self) -> Self:
        self._stack = AsyncExitStack()
        try:
            pool = await self._stack.enter_async_context(MCPSessionPool(command=self._mcp_command))
            if self.agent_id is not None:
                if self.agent_id not in self.specs:
                    raise KeyError(f"unknown agent {self.agent_id!r}; loaded: {sorted(self.specs)}")
                self._runtime = await self._stack.enter_async_context(
                    AgentRuntime(
                        self.specs[self.agent_id],
                        provider=self.provider,
                        workspace=self.workspace,
                        pool=pool,
                        recorder=self.recorder,
                    )
                )
            else:
                self._runtime = await self._stack.enter_async_context(
                    open_team(
                        self.specs,
                        self.root_id,
                        provider=self.provider,
                        workspace=self.workspace,
                        pool=pool,
                        recorder=self.recorder,
                    )
                )
        except BaseException:
            await self._stack.aclose()
            self._stack = None
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
            self._runtime = None

    async def run(self, prompt: str) -> RunResult:
        if self._runtime is None:
            raise RuntimeError("TeamSession is not started; use `async with TeamSession(...)`")
        return await self._runtime.run(prompt)


def parsed_sources(result: RunResult) -> list[str]:
    """Sources from a typed output, when present (for channel rendering)."""
    parsed: Any = result.parsed
    sources = getattr(parsed, "sources", None)
    return list(sources) if isinstance(sources, list) else []
