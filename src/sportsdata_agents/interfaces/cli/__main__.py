"""`agents` CLI entrypoint (Typer).

The operator surface for the platform. Headline commands talk to the agent
team (`run`, `chat`); the rest are deterministic operations — spec tooling
(`lint`, `list`), serving (`serve`, `slack`), the data pipeline (`ingest`,
`results`, `resolve`, `movement`), dictionary stewardship (`steward`,
`dictionary-promote`), the offline eval gate (`eval`), and the ops-plane
console (`ops run`, `ops health`). Run `agents --help` for the full list.
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


@app.command()
def discord() -> None:
    """Run the Discord adapter. Needs DISCORD_BOT_TOKEN + a running gateway
    (`agents serve`). Install the extra: pip install 'sportsdata-agents[discord]'."""
    from dotenv import load_dotenv

    load_dotenv()
    from sportsdata_agents.interfaces.discord.app import serve_bot

    serve_bot()


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
    cron_period: int | None = typer.Option(
        None, "--cron",
        help="Stateless cron pacing: only run feeds whose interval boundary was "
             "crossed in the last N seconds (invoke every N seconds from cron).",
    ),
    prune_days: int | None = typer.Option(None, "--prune", help="Also prune snapshots older than N days."),
    pace: int | None = typer.Option(
        None, "--pace",
        help="Event-proximity floor in seconds: feeds re-capture at least this often "
             "(only ever SPEEDS a feed up; the scheduler sets it as matches approach).",
    ),
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
    from sportsdata_agents.operations.ingestion import (
        FEEDS,
        feeds_due_in_window,
        ingest_once,
        prune_snapshots,
        run_loop,
    )
    from sportsdata_agents.operations.ingestion.worker import INGEST_MAX_BYTES

    console = Console()
    settings = get_settings()
    feeds = list(FEEDS.values()) if feed is None else [FEEDS[feed]]
    from sportsdata_agents.tools.ops import disabled_feeds as _disabled

    skip = _disabled()
    if skip:
        feeds = [f for f in feeds if f.name not in skip]
        console.print(f"[dim]skipping ops-disabled feeds: {', '.join(sorted(skip))}[/dim]")
    if pace is not None and feed is None:
        from dataclasses import replace as _replace

        feeds = [_replace(f, interval_s=min(f.interval_s, pace)) for f in feeds]
        console.print(f"[dim]proximity pace: feeds floored to {pace}s[/dim]")
    if cron_period is not None:
        import time as _time

        feeds = feeds_due_in_window(feeds, now_s=_time.time(), period_s=float(cron_period))
        if not feeds:
            console.print("[dim]no feeds due in this window[/dim]")
            return
    groups = sorted({g for f in feeds for g in f.mcp_groups})

    async def _run() -> None:
        engine = make_engine(settings.database_url)
        async with engine.begin() as conn:  # additive + idempotent; alembic owns prod
            await conn.run_sync(Base.metadata.create_all)
        sf = make_sessionmaker(engine)
        try:
            # ETL has no model context window to protect — lift the MCP byte cap
            # (AU book payloads are MB-scale; see INGEST_MAX_BYTES).
            async with MCPManager(
                groups=groups,
                command=settings.mcp_command,
                extra_env={"SPORTSDATA_MCP_MAX_BYTES": str(INGEST_MAX_BYTES)},
            ) as manager:
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


ops_app = typer.Typer(name="ops", help="Operator console (§3.1): ops-plane agents with platform creds.",
                      no_args_is_help=True)
app.add_typer(ops_app, name="ops")


@ops_app.command(name="run")
def ops_run(
    agent: str = typer.Argument(..., help="An ops-plane agent id (mcp_health, repo_improver, ...)."),
    prompt: str = typer.Argument(..., help="The task for the agent."),
    model: str | None = typer.Option(None, "--model", help="Pin every tier to one litellm model id."),
) -> None:
    """Run an OPS-PLANE agent with platform tools (GitHub, doctor, remediation).

    The only path that injects ops tools — the customer gateway can never reach
    these agents or credentials (§3.1). PRs only; a human merges.
    """
    import asyncio

    from dotenv import load_dotenv

    load_dotenv()

    from sportsdata_agents.agents.loader import load_builtin_specs

    spec = load_builtin_specs().get(agent)
    if spec is None:
        raise typer.BadParameter(f"unknown agent {agent!r}")
    if spec.plane != "ops":
        raise typer.BadParameter(f"{agent!r} is a product-plane agent — use `agents run --agent {agent}`")

    async def _run() -> None:
        from rich.console import Console

        from sportsdata_agents.config import get_settings
        from sportsdata_agents.data.repository import TenantScope
        from sportsdata_agents.gateway.service import (
            TeamSession,
            _default_extra_tools,
            detect_tier_overrides,
            try_db_recorder,
        )
        from sportsdata_agents.interfaces.cli.progress import ConsoleProgressRecorder
        from sportsdata_agents.observability.tracing import setup_observability
        from sportsdata_agents.tools.ops import ops_tools
        from sportsdata_agents.workspace import Workspace

        settings = get_settings()
        setup_observability(settings)
        console = Console()
        tiers = ({"fast": model, "balanced": model, "strong": model} if model
                 else detect_tier_overrides())
        from sportsdata_agents.workspace import Budgets

        # ops runs are platform opex, not tenant spend — the default $0.50/run
        # clamp killed a live improver run; ops specs carry their own ceilings
        workspace = Workspace(tenant_id="platform", workspace_id="ops", model_tiers=tiers,
                              budgets=Budgets(per_run_usd=2.0, monthly_usd=50.0,
                                              max_tool_calls=60, max_steps=60,
                                              max_tokens=200_000, timeout_seconds=2400))
        recorder = ConsoleProgressRecorder(
            console, inner=await try_db_recorder(settings, TenantScope("platform", "ops"))
        )
        inner = getattr(recorder, "inner", None)
        sf = getattr(inner, "session_factory", None)
        extra = _default_extra_tools(recorder) + ops_tools(sf)
        session = TeamSession(settings=settings, workspace=workspace, recorder=recorder,
                              agent_id=agent, extra_tools=extra, allow_ops=True)
        console.print(f"[dim]opening ops agent {agent}…[/dim]")
        async with session:
            result = await session.run(prompt)
        _render_result(console, result)

    asyncio.run(_run())


@ops_app.command(name="health")
def ops_health() -> None:
    """Deterministic platform health: MCP doctor + feed freshness (no LLM)."""
    import asyncio

    from dotenv import load_dotenv

    load_dotenv()

    from rich.console import Console

    console = Console()

    async def _run() -> None:
        from sportsdata_agents.config import get_settings
        from sportsdata_agents.data.db import make_engine, make_sessionmaker
        from sportsdata_agents.tools.ops import ops_tools

        engine = make_engine(get_settings().database_url)
        try:
            tools = {t.name: t for t in ops_tools(make_sessionmaker(engine))}
            doctor = await tools["run_doctor"].execute({})
            console.print(f"doctor: {'✓ ok' if doctor['ok'] else '✗ FAILING'}")
            if not doctor["ok"]:
                console.print(doctor["output"][-2000:])
            health = await tools["feed_health"].execute({"hours": 6})
            console.print(f"providers active (6h): {len(health['providers'])}")
            for stale in health["stale_feeds"]:
                console.print(f"[yellow]stale:[/yellow] {stale['feed']} — {stale['reason']}")
            if health["disabled_feeds"]:
                console.print(f"[dim]disabled: {', '.join(health['disabled_feeds'])}[/dim]")
            if not health["stale_feeds"]:
                console.print("✓ no stale feeds")
            site = await tools["site_status"].execute({})
            if site["ok"]:
                console.print(
                    f"✓ site up ({site['latency_ms']}ms"
                    f"{', playback' if site.get('playback_mode') else ''})"
                )
            else:
                console.print(f"[red]✗ site DOWN:[/red] {site.get('error') or site.get('status_code')}")
        finally:
            await engine.dispose()

    asyncio.run(_run())


@app.command(name="dictionary-promote")
def dictionary_promote(
    write: bool = typer.Option(False, "--write", help="Merge overrides into the packaged seed and clear them."),
) -> None:
    """Promote steward-curated LOCAL dictionary overrides into the packaged seed
    (the file you commit). Without --write it just shows the diff."""
    import json

    from dotenv import load_dotenv

    load_dotenv()

    from importlib import resources

    from rich.console import Console

    from sportsdata_agents.tools.dictionary import _read_overrides, _write_overrides

    console = Console()
    seed_path = resources.files("sportsdata_agents.operations.resolution").joinpath(
        "market_dictionary.json"
    )
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    overrides = _read_overrides()
    rationales = overrides.get("rationales") or {}

    promoted = 0
    for section in ("markets", "sports"):
        for family, aliases in (overrides.get(section) or {}).items():
            for alias in aliases:
                already = alias in (seed.get(section, {}).get(family) or [])
                note = rationales.get(f"{section}:{alias}", "")
                mark = "=" if already else "+"
                console.print(f"{mark} {section}: {alias!r} -> {family!r}"
                              + (f"  [dim]{note}[/dim]" if note else ""))
                if not already:
                    seed.setdefault(section, {}).setdefault(family, []).append(alias)
                    promoted += 1
    if not promoted:
        console.print("[dim]nothing to promote — overrides and seed agree[/dim]")
        return
    if not write:
        console.print(f"\n{promoted} alias(es) would merge — rerun with --write, then commit the seed")
        return
    with open(str(seed_path), "w", encoding="utf-8") as fh:
        json.dump(seed, fh, indent=2, sort_keys=True)
        fh.write("\n")
    _write_overrides({"markets": {}, "sports": {}, "rationales": {}})  # promoted — start clean
    console.print(f"✓ merged {promoted} alias(es) into {seed_path} and cleared the overrides "
                  "(commit the seed change)")


@app.command(name="migrate-warehouse")
def migrate_warehouse_cmd(
    target: str = typer.Argument(..., help="Target database URL (e.g. postgresql+asyncpg://...)."),
    source: str | None = typer.Option(None, "--source", help="Source URL (default: the configured database)."),
    allow_nonempty: bool = typer.Option(False, "--allow-nonempty",
                                        help="Resume into a non-empty target (existing rows are skipped)."),
) -> None:
    """Copy the whole warehouse to another database — the SQLite → Postgres move.

    PAUSE THE INGEST CRON FIRST (comment the */3 line; a live writer shifts the
    copy's pages). After copying, run `alembic upgrade head` against the target: migration 0009
    turns odds_snapshots into a Timescale hypertable with 90-day retention when
    the extension is available (plain Postgres works fine without it). Then point
    SPORTSDATA_AGENTS_DATABASE_URL (and the crontab lines) at the target.
    """
    import asyncio

    from dotenv import load_dotenv

    load_dotenv()

    from rich.console import Console

    from sportsdata_agents.config import get_settings
    from sportsdata_agents.operations.migrate import migrate_warehouse

    console = Console()
    src = source or get_settings().database_url

    async def _run() -> None:
        report = await migrate_warehouse(src, target, allow_nonempty=allow_nonempty)
        for table, count in report.items():
            if table != "total" and count:
                console.print(f"  {table}: {count}")
        console.print(f"✓ migrated {report['total']} rows — now run `alembic upgrade head` "
                      "against the target and repoint SPORTSDATA_AGENTS_DATABASE_URL + crontab")

    asyncio.run(_run())


@app.command()
def monitor(
    watch: str | None = typer.Option(None, "--add", help='Create a watch inline: "name:kind:threshold" '
                                                         '(e.g. "big-moves:line_move:8").'),
    channel: str = typer.Option("log", "--channel", help='Push target for --add: Slack channel id or "log".'),
) -> None:
    """Run one monitoring pass: every active watch scans the price stream since its
    cursor and fires push alerts (M3.2). Deterministic — no LLM. Cron every 5min."""
    import asyncio

    from dotenv import load_dotenv

    load_dotenv()

    from rich.console import Console

    from sportsdata_agents.config import get_settings
    from sportsdata_agents.data.base import Base
    from sportsdata_agents.data.db import make_engine, make_sessionmaker
    from sportsdata_agents.operations.monitoring import run_watches

    console = Console()

    async def _run() -> None:
        engine = make_engine(get_settings().database_url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sf = make_sessionmaker(engine)
        try:
            if watch:
                from sportsdata_agents.data.models import Subscription

                name, kind, threshold = [*watch.split(":"), "5"][:3]
                params_key = {"line_move": "threshold_pct", "steam": "min_moves",
                              "value": "min_edge_pct", "scratching": "stale_minutes",
                              "arb": "threshold_pct"}[kind]
                async with sf() as session:
                    session.add(Subscription(
                        tenant_id="local", workspace_id="local", name=name, kind=kind,
                        params={params_key: float(threshold)}, channel=channel,
                    ))
                    await session.commit()
                console.print(f"✓ watch {name!r} ({kind}, {params_key}={threshold}, channel={channel})")
            report = await run_watches(sf)
            console.print(f"✓ monitor: {report}")
        finally:
            await engine.dispose()

    asyncio.run(_run())


@app.command()
def schedule(
    cron_period: int = typer.Option(60, "--cron", help="Tick window in seconds (cron this every N seconds)."),
    show_status: bool = typer.Option(False, "--status", help="Show per-job state and exit."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what this tick WOULD run and exit."),
) -> None:
    """The conductor: one cron line drives every scheduled job (ingest with
    event-proximity pacing, monitor, nightly settle, weekly ops) and hands
    persistent failures to the incident_triage error agent. Deterministic."""
    import asyncio
    import datetime as _dt

    from dotenv import load_dotenv

    load_dotenv()

    from rich.console import Console

    from sportsdata_agents.operations.scheduler import (
        due_jobs,
        pace_for,
        render_json,
        run_tick,
        seconds_to_nearest_start,
        status,
    )

    console = Console()
    if show_status:
        console.print(render_json(status()))
        return

    async def _pace() -> int | None:
        from sportsdata_agents.config import get_settings
        from sportsdata_agents.data.db import make_engine, make_sessionmaker

        engine = make_engine(get_settings().database_url)
        try:
            return pace_for(await seconds_to_nearest_start(make_sessionmaker(engine)))
        except Exception:  # pacing is an optimisation — a DB hiccup must not stop the tick
            return None
        finally:
            await engine.dispose()

    now = _dt.datetime.now()
    pace = asyncio.run(_pace())
    if dry_run:
        names = [j.name for j in due_jobs(now, float(cron_period))]
        console.print(f"would run: {', '.join(names) or '(nothing due)'}  pace={pace}")
        return
    report = run_tick(now=now, period_s=float(cron_period), pace=pace)
    console.print(
        f"✓ tick: ran={report.ran or '[]'} failed={report.failed or '[]'} "
        f"locked={report.skipped_locked or '[]'} pace={report.pace}"
        + (" health!" if report.health_triggered else "")
        + (f" triage!{report.triage_triggered}" if report.triage_triggered else "")
    )


@app.command()
def steward(
    workspace: str = typer.Option("local", "--workspace", help="Workspace id (tenant scoping + budgets)."),
    model: str | None = typer.Option(None, "--model", help="Pin every tier to one litellm model id."),
) -> None:
    """Run the market_steward's standing audit: map unmapped market names into the
    dictionary (merge safety enforced in the tools). Cron weekly."""
    import asyncio

    prompt = (
        "Run your standing dictionary audit: list the warehouse's unmapped market "
        "names (min_count 20), map the unambiguous ones into the dictionary with "
        "rationales, refuse and report anything ambiguous. Summarise what you "
        "mapped, what you refused, and why."
    )

    async def _run() -> None:
        console, session = await _bootstrap_session(workspace, "market_steward", model)
        console.print("[dim]opening market_steward…[/dim]")
        async with session:
            result = await session.run(prompt)
        _render_result(console, result)

    asyncio.run(_run())


@app.command()
def results() -> None:
    """Settle events: racing placings + league scoreboards (NBA/AFL/NRL finals)
    into event_results, mapped onto fixtures. Deterministic — no LLM. Cron daily."""
    import asyncio

    from dotenv import load_dotenv

    load_dotenv()

    from rich.console import Console

    from sportsdata_agents.config import get_settings
    from sportsdata_agents.data.base import Base
    from sportsdata_agents.data.db import make_engine, make_sessionmaker
    from sportsdata_agents.mcp.manager import MCPManager
    from sportsdata_agents.operations.ingestion.results import (
        ingest_league_results,
        ingest_racing_results,
    )
    from sportsdata_agents.operations.ingestion.worker import INGEST_MAX_BYTES

    console = Console()
    settings = get_settings()

    async def _run() -> None:
        engine = make_engine(settings.database_url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sf = make_sessionmaker(engine)
        try:
            async with MCPManager(
                groups=["pointsbet.racing", "nba.public.cdn", "afl.public.core",
                        "nrl.public.core", "mlb.schedule", "espn.scores"],
                command=settings.mcp_command,
                extra_env={"SPORTSDATA_MCP_MAX_BYTES": str(INGEST_MAX_BYTES)},
            ) as manager:
                racing = await ingest_racing_results(manager, sf)
                league = await ingest_league_results(manager, sf)
            console.print(f"✓ racing: {racing} settled")
            console.print(f"✓ leagues: {league}")
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


@app.command(name="eval")
def eval_cmd(
    baseline: str | None = typer.Option(None, "--baseline", help="Baseline JSON (default: the committed one)."),
    write_baseline: bool = typer.Option(False, "--write-baseline", help="Overwrite the baseline with these scores."),
) -> None:
    """Run the offline eval suite (M2.4) and gate against the baseline.

    Deterministic — no model key, no network. Exit 1 on any regression, so CI can
    answer "did this change make the platform worse?" on every scheduled run.
    """
    import asyncio
    import json
    from pathlib import Path

    from rich.console import Console
    from rich.table import Table

    from sportsdata_agents.evals import gate_against_baseline, load_baseline, run_offline_evals
    from sportsdata_agents.evals.runner import DEFAULT_BASELINE

    console = Console()
    scores = asyncio.run(run_offline_evals())

    table = Table(title="offline evals (higher is better)")
    for col in ("eval", "score", "details"):
        table.add_column(col)
    for s in scores:
        table.add_row(s.name, f"{s.score:.4f}", json.dumps(s.details))
    console.print(table)

    baseline_path = Path(baseline) if baseline else DEFAULT_BASELINE
    if write_baseline:
        baseline_path.write_text(json.dumps({s.name: s.score for s in scores}, indent=2) + "\n")
        console.print(f"[green]baseline written:[/green] {baseline_path}")
        return
    problems = gate_against_baseline(scores, load_baseline(baseline_path))
    for p in problems:
        console.print(f"[red]regression:[/red] {p}")
    if problems:
        raise typer.Exit(1)
    console.print("[green]✓ no regressions against baseline[/green]")


@app.command()
def resolve(
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would map without writing."),
) -> None:
    """Map every book's event ids onto shared fixtures (deterministic, no LLM).

    Run after ingests; cross-book queries (best price, cross-book CLV) join
    through the fixtures/events tables this populates.
    """
    import asyncio

    from dotenv import load_dotenv

    load_dotenv()

    from rich.console import Console

    from sportsdata_agents.config import get_settings
    from sportsdata_agents.data.base import Base
    from sportsdata_agents.data.db import make_engine, make_sessionmaker
    from sportsdata_agents.operations.resolution import resolve_events

    console = Console()

    async def _run() -> None:
        engine = make_engine(get_settings().database_url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        try:
            stats = await resolve_events(make_sessionmaker(engine), dry_run=dry_run)
        finally:
            await engine.dispose()
        mark = "[dim](dry run)[/dim] " if dry_run else ""
        console.print(f"{mark}examined={stats['examined']} mapped={stats['mapped']} "
                      f"created={stats['created']} ambiguous={stats['ambiguous']} "
                      f"unnamed={stats['skipped_unnamed']}")

    asyncio.run(_run())
