"""`agents` CLI entrypoint (Typer).

Scaffold for M0.1 — only `version` is wired today. The headline `run`/`chat` commands
land in M0.12 once the runtime exists.
"""

from __future__ import annotations

import typer

from sportsdata_agents import __version__

app = typer.Typer(
    name="agents",
    help="sportsdata-agents — a configurable team of LLM agents (advisory only).",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _main() -> None:
    """Keep the app in multi-command mode (so `version` is an explicit subcommand)."""


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(f"sportsdata-agents {__version__}")


@app.command()
def lint(
    directory: str | None = typer.Option(None, "--dir", help="Spec directory (default: the bundled specs)."),
) -> None:
    """Validate agent specs (schema + cross-spec checks). Exit 1 on any problem."""
    from pathlib import Path

    from sportsdata_agents.agents.loader import SpecError, builtin_specs_dir, lint_specs, load_specs_dir

    target = Path(directory) if directory else builtin_specs_dir()
    try:
        specs = load_specs_dir(target)
    except SpecError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from e

    if not specs:
        # A lint that "passes" on a typo'd path or empty directory is worse than no lint.
        typer.echo(f"error: no agent specs found in {target}", err=True)
        raise typer.Exit(1)

    problems = lint_specs(specs)
    for p in problems:
        typer.echo(f"error: {p}", err=True)
    if problems:
        raise typer.Exit(1)
    typer.echo(f"✓ lint passed ({len(specs)} agent spec(s))")


@app.command(name="list")
def list_agents() -> None:
    """List the bundled agent specs."""
    from sportsdata_agents.agents.loader import load_builtin_specs

    for spec in load_builtin_specs().values():
        caps = ", ".join(spec.tools.mcp_capabilities) or "—"
        typer.echo(f"{spec.id:20} v{spec.version}  tier={spec.model_tier:9} caps: {caps}")


async def _bootstrap_session(workspace_id: str, agent_id: str | None, model: str | None = None):
    """Shared setup for run/chat: env, observability, recorder, session (unopened)."""
    from dotenv import load_dotenv

    load_dotenv()  # model keys etc. into the process env (litellm reads os.environ)

    from rich.console import Console

    from sportsdata_agents.config import get_settings
    from sportsdata_agents.data.repository import TenantScope
    from sportsdata_agents.gateway.service import TeamSession, detect_tier_overrides, try_db_recorder
    from sportsdata_agents.interfaces.cli.progress import ConsoleProgressRecorder
    from sportsdata_agents.observability.tracing import setup_observability
    from sportsdata_agents.workspace import Workspace

    settings = get_settings()
    setup_observability(settings)
    console = Console()
    tiers = (
        {"fast": model, "balanced": model, "strong": model}
        if model
        else detect_tier_overrides()  # BYO-LLM: use whichever key is configured (§8.1)
    )
    workspace = Workspace(
        tenant_id=settings.default_tenant,
        workspace_id=workspace_id,
        model_tiers=tiers,
    )
    recorder = ConsoleProgressRecorder(
        console, inner=await try_db_recorder(settings, TenantScope(settings.default_tenant, workspace_id))
    )
    session = TeamSession(settings=settings, workspace=workspace, recorder=recorder, agent_id=agent_id)
    return console, session


def _render_result(console, result) -> None:
    from rich.panel import Panel

    from sportsdata_agents.gateway.service import parsed_sources

    answer = result.output or "(no answer)"
    parsed = result.parsed
    if parsed is not None and getattr(parsed, "answer", None):
        answer = parsed.answer
    console.print(Panel(answer, title="answer", border_style="cyan"))
    sources = parsed_sources(result)
    if sources:
        console.print(f"[dim]sources: {', '.join(sources)}[/dim]")
    verified = "" if result.verified is None else f"  verified={result.verified}"
    from sportsdata_agents.agents.grounding import ADVISORY_DISCLAIMER

    console.print(
        f"[dim]stop={result.stop_reason}  steps={result.steps}  tools={result.tool_call_count}  "
        f"cost=${result.cost_usd:.4f}{verified}  ·  {ADVISORY_DISCLAIMER}[/dim]"
    )


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8400, "--port"),
) -> None:
    """Run the HTTP gateway (channel-agnostic POST /message + async tasks + SSE)."""
    from dotenv import load_dotenv

    load_dotenv()
    from sportsdata_agents.gateway.app import serve as _serve

    _serve(host=host, port=port)


@app.command()
def slack() -> None:
    """Run the Slack adapter (Socket Mode). Needs SLACK_BOT_TOKEN + SLACK_APP_TOKEN."""
    from dotenv import load_dotenv

    load_dotenv()
    from sportsdata_agents.interfaces.slack.app import serve_socket_mode

    serve_socket_mode()


@app.command(name="refresh-books")
def refresh_books_cmd() -> None:
    """Re-verify bookmaker catalogue ids and rewrite book_navigation's auto section.

    Deterministic (no LLM). Run weekly — cron example:
    `0 6 * * 1  cd <repo> && .venv/bin/agents refresh-books`
    """
    import asyncio

    from dotenv import load_dotenv

    load_dotenv()

    from rich.console import Console

    from sportsdata_agents.operations.refresh_books import refresh_books, summary_lines

    console = Console()
    catalogue = asyncio.run(refresh_books())
    console.print("[green]book catalogue refreshed:[/green]")
    for line in summary_lines(catalogue):
        console.print(f"  {line}")


@app.command()
def run(
    prompt: str = typer.Argument(..., help="The question/task for the team."),
    workspace: str = typer.Option("local", "--workspace", help="Workspace id (tenant scoping + budgets)."),
    agent: str | None = typer.Option(None, "--agent", help="Run a single agent instead of the team."),
    model: str | None = typer.Option(None, "--model", help="Pin every tier to one litellm model id."),
) -> None:
    """Ask the agent team one question and print the answer (with sources + cost)."""
    import asyncio

    async def _run() -> None:
        console, session = await _bootstrap_session(workspace, agent, model)
        console.print(f"[dim]opening {session.agent_name}…[/dim]")
        async with session:
            result = await session.run(prompt)
        _render_result(console, result)

    asyncio.run(_run())


@app.command()
def chat(
    workspace: str = typer.Option("local", "--workspace", help="Workspace id (tenant scoping + budgets)."),
    agent: str | None = typer.Option(None, "--agent", help="Chat with a single agent instead of the team."),
    model: str | None = typer.Option(None, "--model", help="Pin every tier to one litellm model id."),
) -> None:
    """Interactive REPL with the team (sessions stay warm; /exit to quit).

    Turns are independent for now — cross-turn memory lands with the memory service.
    """
    import asyncio

    async def _chat() -> None:
        console, session = await _bootstrap_session(workspace, agent, model)
        console.print(f"[dim]opening {session.agent_name}… (/exit to quit)[/dim]")
        async with session:
            while True:
                try:
                    prompt = (await asyncio.to_thread(console.input, "[bold cyan]you>[/bold cyan] ")).strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not prompt:
                    continue
                if prompt in {"/exit", "/quit", "exit", "quit"}:
                    break
                result = await session.run(prompt)
                _render_result(console, result)
        console.print("[dim]bye[/dim]")

    asyncio.run(_chat())


if __name__ == "__main__":  # pragma: no cover
    app()


@app.command()
def ingest(
    once: bool = typer.Option(True, "--once/--loop", help="One capture (default) or the scheduled loop."),
    feed: str | None = typer.Option(None, "--feed", help="Run a single feed by name."),
    prune_days: int | None = typer.Option(None, "--prune", help="Also prune snapshots older than N days."),
) -> None:
    """Capture odds into the history warehouse (M2.1). Deterministic — no LLM.

    Uses SPORTSDATA_AGENTS_DATABASE_URL (SQLite urls work: the schema is ensured
    on startup). Cron the --once form, or run --loop for per-feed schedules.
    """
    import asyncio

    from dotenv import load_dotenv

    load_dotenv()

    from rich.console import Console

    from sportsdata_agents.config import get_settings
    from sportsdata_agents.data.base import Base
    from sportsdata_agents.data.db import make_engine, make_sessionmaker
    from sportsdata_agents.mcp.manager import MCPManager
    from sportsdata_agents.operations.ingestion import FEEDS, ingest_once, prune_snapshots, run_loop

    console = Console()
    settings = get_settings()
    feeds = list(FEEDS.values()) if feed is None else [FEEDS[feed]]
    groups = sorted({g for f in feeds for g in f.mcp_groups})

    async def _run() -> None:
        engine = make_engine(settings.database_url)
        async with engine.begin() as conn:  # additive + idempotent; alembic owns prod
            await conn.run_sync(Base.metadata.create_all)
        sf = make_sessionmaker(engine)
        try:
            async with MCPManager(groups=groups, command=settings.mcp_command) as manager:
                if once:
                    report = await ingest_once(manager, sf, feeds)
                    for name, stats in report.items():
                        mark = "✓" if stats.get("ok") else "✗"
                        console.print(f"{mark} {name}: {stats}")
                else:
                    console.print(f"[dim]ingestion loop: {', '.join(f.name for f in feeds)} (ctrl-c to stop)[/dim]")
                    await run_loop(manager, sf, feeds)
            if prune_days is not None:
                pruned = await prune_snapshots(sf, older_than_days=prune_days)
                console.print(f"pruned {pruned} snapshots older than {prune_days}d")
        finally:
            await engine.dispose()

    asyncio.run(_run())


@app.command()
def movement(
    event: str = typer.Argument(..., help="Event external id (e.g. an NBA gameId)."),
    market: str | None = typer.Option(None, "--market"),
    selection: str | None = typer.Option(None, "--selection"),
    book: str | None = typer.Option(None, "--book"),
) -> None:
    """Show line movement for an event from the warehouse (change-points)."""
    import asyncio

    from dotenv import load_dotenv

    load_dotenv()

    from rich.console import Console
    from rich.table import Table

    from sportsdata_agents.config import get_settings
    from sportsdata_agents.data.db import make_engine, make_sessionmaker
    from sportsdata_agents.operations.ingestion import line_movement

    console = Console()

    async def _run() -> None:
        engine = make_engine(get_settings().database_url)
        try:
            rows = await line_movement(
                make_sessionmaker(engine), event_external_id=event, market=market, selection=selection, book=book
            )
        finally:
            await engine.dispose()
        if not rows:
            console.print("no price history for that event")
            return
        table = Table(title=f"line movement — {event}")
        for col in ("changed_at", "book", "market", "selection", "prev_odds", "odds"):
            table.add_column(col)
        for r in rows:
            table.add_row(r["changed_at"], r["book"], r["market"], r["selection"], str(r["prev_odds"]), str(r["odds"]))
        console.print(table)

    asyncio.run(_run())
