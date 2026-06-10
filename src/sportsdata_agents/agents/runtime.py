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

from sportsdata_agents.agents.grounding import grounding_verifier
from sportsdata_agents.agents.harness import (
    CURRENT_RUN_BUDGET,
    CompletionProvider,
    Harness,
    RunResult,
    ToolDef,
    Verifier,
)
from sportsdata_agents.agents.skills import load_skillset
from sportsdata_agents.agents.spec import AgentSpec
from sportsdata_agents.mcp.manager import MCPManager
from sportsdata_agents.mcp.pool import MCPSessionPool
from sportsdata_agents.mcp.toolset import bridge_mcp_tools
from sportsdata_agents.models.gateway import RunBudget
from sportsdata_agents.observability.recorder import RunRecorder
from sportsdata_agents.workspace import Workspace


def _db_unavailable_stub(name: str) -> ToolDef:
    """Stand-in for a DB-backed tool when no database is configured: the agent gets a
    clear, actionable error instead of the team failing to open."""

    async def execute(args: dict) -> str:
        return (
            f"error: {name} is not configured in this session — it needs the database "
            f"(docker compose up -d && alembic upgrade head) or, for Slack tools, SLACK_BOT_TOKEN"
        )

    return ToolDef(
        name=name,
        description=f"(unavailable: requires the database) {name}",
        parameters={"type": "object", "properties": {}},
        execute=execute,
    )


def delegate_tool(runtime: AgentRuntime) -> ToolDef:
    """Expose a runtime as a tool: the §8.2 delegation pattern. The sub-agent runs in
    its own context; only a condensed summary returns to the caller's window."""
    spec = runtime.spec

    async def execute(args: dict) -> str:
        task = str(args.get("task", "")).strip()
        if not task:
            return "error: delegation requires a non-empty `task`"
        # Charge the CALLER's budget: a team run shares one per-run ceiling (§16.1) —
        # otherwise "per-run" would multiply by the number of delegations.
        result = await runtime.run(task, budget=CURRENT_RUN_BUDGET.get())
        summary: dict = {
            "agent": spec.id,
            "answer": result.output,
            "stop_reason": result.stop_reason,
            "verified": result.verified,
        }
        if result.parsed is not None:
            # Typed outputs must survive the delegation boundary as STRUCTURE, not a
            # double-encoded string — that's what they're for (M0.9).
            summary["data"] = result.parsed.model_dump(mode="json")
        return json.dumps(summary)

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
        pool: MCPSessionPool | None = None,
        delegates: Sequence[AgentRuntime] = (),
        extra_tools: Sequence[ToolDef] = (),
        skills_root: Path | None = None,
        verifier: Verifier | None = None,
        recorder: RunRecorder | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.spec = spec
        self.provider = provider
        self.workspace = workspace
        self._mcp_command = mcp_command
        self._pool = pool
        self._owns_manager = False
        self._delegates = list(delegates)
        # The spec is the ACL: a runtime must not bind delegates the spec never declared.
        undeclared = [d.spec.id for d in self._delegates if d.spec.id not in spec.can_delegate_to]
        if undeclared:
            raise ValueError(f"{spec.id}: delegates {undeclared} are not declared in can_delegate_to (§13)")
        self._extra_tools = {t.name: t for t in extra_tools}
        self._skills_root = skills_root
        self._verifier = verifier
        self._recorder = recorder
        self._now = now
        self._manager: MCPManager | None = None
        self.harness: Harness | None = None

    async def __aenter__(self) -> Self:
        try:
            tools: list[ToolDef] = []
            if self.spec.tools.native:
                if "run_python" in self.spec.tools.native and self.spec.sandbox != "ephemeral":
                    raise ValueError(
                        f"{self.spec.id}: run_python requires `sandbox: ephemeral` in the spec (§10)"
                    )
                # registry first; session-bound extras (e.g. DB-backed tracking) second;
                # KNOWN session tools degrade to an actionable stub when the DB is
                # absent (a DB-less team must still OPEN — try_db_recorder philosophy).
                from sportsdata_agents.tools.memory import MEMORY_TOOL_NAMES
                from sportsdata_agents.tools.quant import QUANT_TOOL_NAMES
                from sportsdata_agents.tools.registry import NATIVE_TOOLS
                from sportsdata_agents.tools.slack_admin import SLACK_ADMIN_TOOL_NAMES
                from sportsdata_agents.tools.tracking import TRACKING_TOOL_NAMES

                session_tool_names = (
                    TRACKING_TOOL_NAMES | MEMORY_TOOL_NAMES | SLACK_ADMIN_TOOL_NAMES | QUANT_TOOL_NAMES
                )
                for name in self.spec.tools.native:
                    if name in NATIVE_TOOLS:
                        tools.append(NATIVE_TOOLS[name])
                    elif name in self._extra_tools:
                        tools.append(self._extra_tools[name])
                    elif name in session_tool_names:
                        tools.append(_db_unavailable_stub(name))
                    else:
                        raise KeyError(
                            f"unknown native tool {name!r}; registered: "
                            f"{sorted(set(NATIVE_TOOLS) | set(self._extra_tools))}"
                        )

            needs_mcp = bool(self.spec.tools.mcp_capabilities or self.spec.tools.mcp_groups)
            if needs_mcp:
                # Subprocess scope: the spec's groups, else the workspace ceiling (§13).
                groups = list(self.spec.tools.mcp_groups) or list(self.workspace.mcp_groups)
                if self._pool is not None:
                    # Borrowed: the pool owns it — identical scopes share one subprocess.
                    self._manager = await self._pool.get(groups)
                    self._owns_manager = False
                else:
                    self._manager = MCPManager(groups=groups, command=self._mcp_command)
                    await self._manager.__aenter__()
                    self._owns_manager = True
                tools.extend(await bridge_mcp_tools(self._manager, self.spec.tools.mcp_capabilities or None))

            for sub in self._delegates:
                tools.append(delegate_tool(sub))

            skills = load_skillset(list(self.spec.skills), self._skills_root) if self.spec.skills else None
            # context.verify without an explicit verifier gets the grounding check (§13.1).
            verifier = self._verifier
            if verifier is None and self.spec.context.verify:
                verifier = grounding_verifier
            self.harness = Harness(
                self.spec,
                provider=self.provider,
                workspace=self.workspace,
                tools=tools,
                skills=skills,
                verifier=verifier,
                recorder=self._recorder,
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
            if self._manager is not None and self._owns_manager:
                await self._manager.__aexit__(exc_type, exc, tb)
        finally:
            self._manager = None
            self._owns_manager = False
            self.harness = None

    async def run(
        self, task: str, *, budget: RunBudget | None = None, recorder: RunRecorder | None = None
    ) -> RunResult:
        if self.harness is None:
            raise RuntimeError("AgentRuntime is not started; use `async with AgentRuntime(...)`")
        return await self.harness.run(task, budget=budget, recorder=recorder)


@asynccontextmanager
async def open_team(
    specs: dict[str, AgentSpec],
    root_id: str,
    *,
    provider: CompletionProvider,
    workspace: Workspace,
    mcp_command: Sequence[str] | None = None,
    pool: MCPSessionPool | None = None,
    extra_tools: Sequence[ToolDef] = (),
    skills_root: Path | None = None,
    verifier: Verifier | None = None,
    recorder: RunRecorder | None = None,
) -> AsyncIterator[AgentRuntime]:
    """Open the root agent with its delegates bound (one level deep — the Tier-0 model:
    the orchestrator delegates to specialists; specialists do not delegate further).
    Agents with identical MCP scopes share one subprocess via the session pool."""
    root_spec = specs[root_id]
    async with AsyncExitStack() as stack:
        if pool is None:
            pool = await stack.enter_async_context(MCPSessionPool(command=mcp_command))
        delegates: list[AgentRuntime] = []
        for target_id in root_spec.can_delegate_to:
            if target_id not in specs:
                raise KeyError(f"{root_id} delegates to unknown agent {target_id!r}")
            if specs[target_id].can_delegate_to:
                # Silently unbound delegation would surface as confusing unknown-tool
                # errors at runtime; fail at build time instead.
                raise ValueError(
                    f"{target_id} declares can_delegate_to but open_team wires one level only "
                    f"(orchestrator → specialists); nested delegation is not supported yet"
                )
            sub = AgentRuntime(
                specs[target_id],
                provider=provider,
                workspace=workspace,
                mcp_command=mcp_command,
                pool=pool,
                extra_tools=extra_tools,
                skills_root=skills_root,
                verifier=verifier,
                recorder=recorder,
            )
            delegates.append(await stack.enter_async_context(sub))
        root = AgentRuntime(
            root_spec,
            provider=provider,
            workspace=workspace,
            mcp_command=mcp_command,
            pool=pool,
            delegates=delegates,
            extra_tools=extra_tools,
            skills_root=skills_root,
            verifier=verifier,
            recorder=recorder,
        )
        yield await stack.enter_async_context(root)
