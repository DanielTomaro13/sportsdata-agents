"""Agent runtime: bind a spec to live infrastructure, and compose a team (§6/§8).

``AgentRuntime`` turns one :class:`AgentSpec` into a runnable harness: a **scoped MCP
session** (least privilege, §13), capability-bridged tools, native tools, skills, and
delegate tools for sub-agents. ``open_team`` wires the one-level tree the plan's Tier-0
model describes: an orchestrator whose only tools are its **specialists-as-tools** —
each delegation runs in the specialist's own context and returns a condensed summary
(§8.2 sub-agent isolation).
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from types import TracebackType
from typing import Self

from sportsdata_agents.agents.harness import CompletionProvider, Harness, RunResult, ToolDef, Verifier
from sportsdata_agents.agents.skills import load_skillset
from sportsdata_agents.agents.spec import AgentSpec
from sportsdata_agents.mcp.manager import MCPManager
from sportsdata_agents.mcp.toolset import bridge_mcp_tools
from sportsdata_agents.tools.registry import get_native_tools
from sportsdata_agents.workspace import Workspace


def delegate_tool(runtime: AgentRuntime) -> ToolDef:
    """Expose a runtime as a tool: the §8.2 delegation pattern. The sub-agent runs in
    its own context; only a condensed summary returns to the caller's window."""
    spec = runtime.spec

    async def execute(args: dict) -> str:
        task = str(args.get("task", "")).strip()
        if not task:
            return "error: delegation requires a non-empty `task`"
        result = await runtime.run(task)
        return json.dumps(
            {
                "agent": spec.id,
                "answer": result.output,
                "stop_reason": result.stop_reason,
                "verified": result.verified,
            }
        )

    return ToolDef(
        name=spec.id,
        description=f"Delegate a task to {spec.display_name}: {spec.description or spec.system_prompt[:100]}",
        parameters={
            "type": "object",
            "properties": {"task": {"type": "string", "description": "The question/task for this specialist."}},
            "required": ["task"],
        },
        execute=execute,
    )


class AgentRuntime:
    """One spec, bound and runnable. Use as an async context manager."""

    def __init__(
        self,
        spec: AgentSpec,
        *,
        provider: CompletionProvider,
        workspace: Workspace,
        mcp_command: Sequence[str] | None = None,
        delegates: Sequence[AgentRuntime] = (),
        skills_root: Path | None = None,
        verifier: Verifier | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.spec = spec
        self.provider = provider
        self.workspace = workspace
        self._mcp_command = mcp_command
        self._delegates = list(delegates)
        self._skills_root = skills_root
        self._verifier = verifier
        self._now = now
        self._manager: MCPManager | None = None
        self.harness: Harness | None = None

    async def __aenter__(self) -> Self:
        try:
            tools: list[ToolDef] = []
            if self.spec.tools.native:
                tools.extend(get_native_tools(self.spec.tools.native))

            needs_mcp = bool(self.spec.tools.mcp_capabilities or self.spec.tools.mcp_groups)
            if needs_mcp:
                # Subprocess scope: the spec's groups, else the workspace ceiling (§13).
                groups = list(self.spec.tools.mcp_groups) or list(self.workspace.mcp_groups)
                self._manager = MCPManager(groups=groups, command=self._mcp_command)
                await self._manager.__aenter__()
                tools.extend(await bridge_mcp_tools(self._manager, self.spec.tools.mcp_capabilities or None))

            for sub in self._delegates:
                tools.append(delegate_tool(sub))

            skills = load_skillset(list(self.spec.skills), self._skills_root) if self.spec.skills else None
            self.harness = Harness(
                self.spec,
                provider=self.provider,
                workspace=self.workspace,
                tools=tools,
                skills=skills,
                verifier=self._verifier,
                now=self._now,
            )
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if self._manager is not None:
                await self._manager.__aexit__(exc_type, exc, tb)
        finally:
            self._manager = None
            self.harness = None

    async def run(self, task: str) -> RunResult:
        if self.harness is None:
            raise RuntimeError("AgentRuntime is not started; use `async with AgentRuntime(...)`")
        return await self.harness.run(task)


@asynccontextmanager
async def open_team(
    specs: dict[str, AgentSpec],
    root_id: str,
    *,
    provider: CompletionProvider,
    workspace: Workspace,
    mcp_command: Sequence[str] | None = None,
    skills_root: Path | None = None,
    verifier: Verifier | None = None,
) -> AsyncIterator[AgentRuntime]:
    """Open the root agent with its delegates bound (one level deep — the Tier-0 model:
    the orchestrator delegates to specialists; specialists do not delegate further)."""
    root_spec = specs[root_id]
    async with AsyncExitStack() as stack:
        delegates: list[AgentRuntime] = []
        for target_id in root_spec.can_delegate_to:
            if target_id not in specs:
                raise KeyError(f"{root_id} delegates to unknown agent {target_id!r}")
            sub = AgentRuntime(
                specs[target_id],
                provider=provider,
                workspace=workspace,
                mcp_command=mcp_command,
                skills_root=skills_root,
                verifier=verifier,
            )
            delegates.append(await stack.enter_async_context(sub))
        root = AgentRuntime(
            root_spec,
            provider=provider,
            workspace=workspace,
            mcp_command=mcp_command,
            delegates=delegates,
            skills_root=skills_root,
            verifier=verifier,
        )
        yield await stack.enter_async_context(root)
