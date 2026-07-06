"""`agents` CLI entrypoint (Typer).

The operator surface for the platform. Headline commands talk to the agent
team (`run`, `chat`); the rest are deterministic operations — spec tooling
(`lint`, `list`), serving (`serve`, `slack`), the data pipeline (`ingest`,
`results`, `resolve`, `movement`), dictionary stewardship (`steward`,
`dictionary-promote`), the offline eval gate (`eval`), and the ops-plane
console (`ops run`, `ops health`). Run `agents --help` for the full list.
"""

from __future__ import annotations

from typing import Any

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
    demo_only: bool = typer.Option(
        False, "--demo-only",
        help="Expose ONLY /healthz, /demo/* and /leads — the abuse-hardened public "
             "surface, and the only mode meant to face the internet (the full "
             "gateway is the localhost desktop daemon).",
    ),
) -> None:
    """Run the HTTP gateway (channel-agnostic POST /message + async tasks + SSE)."""
    from dotenv import load_dotenv

    load_dotenv()
    if not demo_only:  # the public demo surface is always allowed; the full chat gateway is gated
        _require_entitlement("chat_ui")
    from sportsdata_agents.gateway.app import serve as _serve

    _serve(host=host, port=port, demo_only=demo_only)


def _require_addon(name: str) -> None:
    from sportsdata_agents.licensing.enforce import EntitlementError, require_addon

    try:
        require_addon(name)
    except EntitlementError as e:
        from rich.console import Console

        Console().print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e


def _require_entitlement(feature: str) -> None:
    from sportsdata_agents.licensing.enforce import (
        EntitlementError,
        require_chat_ui,
        require_full_app,
    )

    check = {"chat_ui": require_chat_ui, "full_app": require_full_app}[feature]
    try:
        check()
    except EntitlementError as e:
        from rich.console import Console

        Console().print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e


@app.command()
def license(
    activate: str | None = typer.Option(None, "--activate", help="Paste a license key to activate it."),
    refresh: bool = typer.Option(False, "--refresh",
                                 help="Fetch your latest licence (renewals) from the billing server."),
    refresh_url: str | None = typer.Option(None, "--refresh-url",
                                           help="Override SPORTSDATA_LICENSE_REFRESH_URL."),
) -> None:
    """Show the current tier and entitlements, activate a license key, or refresh
    a subscription licence (picks up the token your last renewal minted)."""
    import os

    from rich.console import Console

    from sportsdata_agents.licensing import current_entitlements, verify_license
    from sportsdata_agents.licensing.license import KEYCHAIN_LICENSE_NAME, _token_from_sources
    from sportsdata_agents.secrets import set_keychain_secret

    console = Console()

    def _store(token: str) -> None:
        if not set_keychain_secret(KEYCHAIN_LICENSE_NAME, token):
            from sportsdata_agents.paths import data_dir

            (data_dir() / "license.key").write_text(token, encoding="utf-8")

    if refresh:
        url = refresh_url or os.environ.get("SPORTSDATA_LICENSE_REFRESH_URL")
        if not url:
            console.print("[red]no refresh URL[/red] — pass --refresh-url or set "
                          "SPORTSDATA_LICENSE_REFRESH_URL")
            raise typer.Exit(1)
        current = _token_from_sources()
        if not current:
            console.print("[red]no licence on this machine to refresh[/red] — activate one first")
            raise typer.Exit(1)
        import json as _json
        import urllib.request

        req = urllib.request.Request(url, data=_json.dumps({"token": current}).encode(),
                                     headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                doc = _json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            console.print(f"[red]refresh failed:[/red] {e}")
            raise typer.Exit(1) from e
        token = str(doc.get("token", "")).strip()
        try:
            claims = verify_license(token)
        except Exception as e:
            console.print(f"[red]the refreshed key did not verify:[/red] {e}")
            raise typer.Exit(1) from e
        _store(token)
        console.print(f"[green]✓ refreshed[/green] — {claims.tier.upper()} tier"
                      + (f", expires {claims.expires}" if claims.expires else ""))
        return

    if activate:
        try:
            claims = verify_license(activate.strip())
        except Exception as e:
            console.print(f"[red]that key did not verify:[/red] {e}")
            raise typer.Exit(1) from e
        _store(activate.strip())
        console.print(f"[green]✓ activated[/green] — {claims.tier.upper()} tier for {claims.issued_to}"
                      + (f", expires {claims.expires}" if claims.expires else ""))
        return

    ent = current_entitlements()
    console.print(f"[bold]Tier:[/bold] {ent.tier.upper()}")
    quota = "unlimited" if ent.effective_mcp_quota() < 0 else ent.effective_mcp_quota()
    console.print(f"  MCP quota: {quota}")
    console.print(f"  Chat interface: {'yes' if ent.chat_ui else 'no'}")
    console.print(f"  Desktop app: {'yes' if ent.full_app else 'no'}")
    console.print(f"  Agents: {'all' if ent.agents is None else ', '.join(ent.agents)}")
    console.print(f"  Add-ons: {', '.join(sorted(ent.addons)) or 'none'}")
    if ent.note:
        console.print(f"  [dim]{ent.note}[/dim]")


@app.command()
def billing(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host (put a TLS proxy in front)."),
    port: int = typer.Option(8090, "--port"),
) -> None:
    """Run the payment webhook → license issuer (the one small server). Needs
    SPORTSDATA_LICENSE_PRIVKEY, SPORTSDATA_BILLING_PRODUCTS, and the per-provider
    *_WEBHOOK_SECRET. Point Paddle/LemonSqueezy at /webhook/<provider>."""
    import os

    import uvicorn
    from dotenv import load_dotenv
    from rich.console import Console

    load_dotenv()
    if not os.environ.get("SPORTSDATA_LICENSE_PRIVKEY"):
        Console().print(
            "[red]SPORTSDATA_LICENSE_PRIVKEY not set[/red] — generate a keypair with "
            "`python scripts/license.py keygen` and bake the public half into the build."
        )
        raise typer.Exit(1)

    from sportsdata_agents.licensing.billing import create_billing_app, product_map

    providers = list(product_map().keys())
    if not providers:
        Console().print(
            "[yellow]SPORTSDATA_BILLING_PRODUCTS is empty[/yellow] — no products are mapped to "
            "tiers, so every webhook will 400. See POST_DEV for the JSON shape."
        )
    Console().print(f"billing webhook listening on http://{host}:{port}  providers={providers or '—'}")
    uvicorn.run(create_billing_app(), host=host, port=port)


@app.command()
def setup(
    check: bool = typer.Option(False, "--check", help="Exit 0 if a model key is configured, 1 if not (no prompts)."),
) -> None:
    """First-run wizard: pick a model provider, store its key in the OS keychain.
    Run once on a fresh desktop install (the app prompts you if it's missing)."""
    import asyncio

    from dotenv import load_dotenv
    from rich.console import Console
    from rich.prompt import Prompt

    from sportsdata_agents.app.wizard import PROVIDERS, configured_provider, store_key, verify_key
    from sportsdata_agents.paths import data_dir

    load_dotenv()
    if check:
        # Non-interactive probe for the .app launcher: is a key already set up?
        raise typer.Exit(0 if configured_provider() else 1)

    console = Console()
    console.print(f"[bold]sportsdata setup[/bold] — data lives in [cyan]{data_dir()}[/cyan]\n")

    already = configured_provider()
    if already:
        console.print(f"[green]✓[/green] a key for [bold]{already.label}[/bold] is already configured.")
        if Prompt.ask("Reconfigure?", choices=["y", "n"], default="n") == "n":
            return

    for i, provider in enumerate(PROVIDERS, 1):
        tag = " [green](free tier)[/green]" if provider.free_tier else ""
        console.print(f"  {i}. {provider.label}{tag} — [dim]{provider.hint}[/dim]")
    choice = int(Prompt.ask("Pick a provider", choices=[str(i) for i in range(1, len(PROVIDERS) + 1)],
                            default="1"))
    provider = PROVIDERS[choice - 1]
    key = Prompt.ask(f"Paste your {provider.label} API key", password=True).strip()
    if not key:
        console.print("[red]no key entered — aborting[/red]")
        raise typer.Exit(1)

    console.print("[dim]verifying with a live call…[/dim]")
    ok, detail = asyncio.run(verify_key(provider, key))
    if not ok:
        console.print(f"[red]✗ that key did not work:[/red] {detail}")
        raise typer.Exit(1)

    where = store_key(provider, key)
    if where == "keychain":
        console.print(f"[green]✓ verified and stored in the OS keychain[/green] ({provider.key_env}).")
    else:
        console.print(f"[yellow]verified, but no keychain available[/yellow] — set "
                      f"[bold]{provider.key_env}[/bold] in your environment or .env.")

    from sportsdata_agents.paths import desk_dir, set_desk_dir

    default_desk = desk_dir()
    chosen = Prompt.ask("\nDesk folder for exports (boards, CSVs, reports)", default=str(default_desk))
    if chosen and chosen != str(default_desk):
        set_desk_dir(chosen)
        console.print(f"[green]✓ desk folder set to[/green] [cyan]{chosen}[/cyan]")
    console.print("\nRun [bold]agents app[/bold] to start the desktop daemon.")


@app.command()
def config(
    verify: bool = typer.Option(False, "--verify", help="Also make one live model call to test the key."),
) -> None:
    """Inventory + validate the whole backend config — what's set, missing, or just
    informational — grouped by what it's for. One screen of operator preflight."""
    from rich.console import Console

    from sportsdata_agents.operations.preflight import run_preflight, summarise

    console = Console()
    checks = run_preflight(verify=verify)
    icon = {"ok": "[green]✓[/green]", "warn": "[yellow]●[/yellow]",
            "missing": "[red]✗[/red]", "info": "[dim]·[/dim]"}
    last_group = ""
    for c in checks:
        if c.group != last_group:
            console.print(f"\n[bold]{c.group}[/bold]")
            last_group = c.group
        console.print(f"  {icon[c.status]} {c.label:30} [dim]{c.detail}[/dim]")
    s = summarise(checks)
    console.print(f"\n[bold]{s['ok']} ok[/bold] · {s['warn']} warn · "
                  f"[red]{s['missing']} missing[/red] · {s['info']} info")


@app.command()
def costs(
    days: int = typer.Option(7, "--days", help="Window for the spend report."),
    set_budget: float | None = typer.Option(None, "--set-budget", help="Set a spend cap (USD)."),
    period: str = typer.Option("monthly", "--period", help="Budget period: daily | weekly | monthly."),
) -> None:
    """Model spend — by day, agent and model, ops vs product — against your budget.
    `--set-budget 50 --period monthly` sets a cap. The cap is ENFORCED, not just
    reported: once the period's spend reaches it, the model gateway refuses further
    calls (runs end as `budget_exhausted`) until the period rolls over."""
    import asyncio

    from rich.console import Console

    from sportsdata_agents.config import get_settings
    from sportsdata_agents.data.db import make_engine, make_sessionmaker
    from sportsdata_agents.operations import costs as cost_mod

    console = Console()
    if set_budget is not None:
        try:
            b = cost_mod.set_budget(set_budget, period)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1) from e
        console.print(f"[green]✓ budget set[/green] — ${b['cap_usd']:.2f} / {b['period']} "
                      f"[dim](enforced: model calls are refused once this is spent)[/dim]")
        return

    async def _run() -> tuple[dict[str, Any], dict[str, Any] | None]:
        engine = make_engine(get_settings().database_url)
        try:
            sf = make_sessionmaker(engine)
            return await cost_mod.spend_report(sf, days=days), await cost_mod.budget_status(sf)
        finally:
            await engine.dispose()

    report, budget = asyncio.run(_run())
    console.print(f"[bold]Spend (last {days}d):[/bold] ${report['total_usd']:.4f}  "
                  f"[dim]({report['runs']} runs · ops ${report['ops_usd']:.4f} · "
                  f"product ${report['product_usd']:.4f})[/dim]")
    if budget:
        colour = "red" if budget["breached"] else ("yellow" if budget["pct"] >= 80 else "green")
        console.print(f"[bold]Budget:[/bold] ${budget['spent_usd']:.2f} / ${budget['cap_usd']:.2f} "
                      f"this {budget['period']} — [{colour}]{budget['pct']:.0f}%"
                      f"{' · OVER BUDGET' if budget['breached'] else ''}[/{colour}]")
    else:
        console.print("[dim]no budget set — `agents costs --set-budget <USD>`[/dim]")
    if report["by_agent"]:
        console.print("\n[bold]Top agents:[/bold]")
        for agent, v in list(report["by_agent"].items())[:8]:
            err = f" [red]{v['errors']} err[/red]" if v["errors"] else ""
            console.print(f"  {agent:24} ${v['cost']:.4f}  [dim]{v['runs']} runs{err}[/dim]")
    if report["by_model"]:
        console.print("\n[bold]By model:[/bold]")
        for model, c in list(report["by_model"].items())[:6]:
            console.print(f"  {model:36} ${c:.4f}")


@app.command()
def skills(
    remove: str | None = typer.Option(None, "--remove", help="Delete a learned skill by name (built-ins protected)."),
) -> None:
    """List every skill the platform knows — the built-in playbooks plus the ones
    the generalist has authored as it learned your needs. --remove prunes one."""
    import asyncio

    from rich.console import Console

    from sportsdata_agents.tools.skillsmith import list_skills as _list_skills
    from sportsdata_agents.tools.skillsmith import remove_skill

    console = Console()
    if remove:
        try:
            res = remove_skill(remove)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1) from e
        if res["removed"]:
            console.print(f"[green]✓ removed[/green] learned skill [bold]{res['name']}[/bold]")
        else:
            console.print(f"[yellow]no learned skill named[/yellow] {res['name']!r}")
        return

    result = asyncio.run(_list_skills({}))
    learned = [s for s in result["skills"] if s["source"] == "user"]
    builtin = [s for s in result["skills"] if s["source"] == "builtin"]
    if learned:
        console.print("[bold]Learned skills[/bold] (authored by the generalist):")
        for s in learned:
            console.print(f"  [green]{s['name']}[/green] — {s['description']}")
        console.print()
    console.print("[bold]Built-in skills:[/bold]")
    for s in builtin:
        console.print(f"  {s['name']} — [dim]{s['description']}[/dim]")
    if not learned:
        console.print("\n[dim]No learned skills yet — the generalist writes one when it cracks a "
                      "reusable method for a request no specialist covered.[/dim]")


@app.command()
def desk(
    set_path: str | None = typer.Option(None, "--set", help="Set the desk folder to this path (persisted)."),
) -> None:
    """Show (or set) the desk folder — where agents export boards, CSVs and
    reports for you to open. The Cursor-workspace equivalent."""
    import os

    from rich.console import Console

    from sportsdata_agents.paths import desk_dir, set_desk_dir

    console = Console()

    if set_path:
        resolved = set_desk_dir(set_path)
        console.print(f"[green]✓ desk folder set to[/green] [cyan]{resolved}[/cyan]")
        return
    current = desk_dir()
    console.print(f"desk folder: [cyan]{current}[/cyan]")
    if os.environ.get("SPORTSDATA_AGENTS_DESK_DIR"):
        console.print("[dim](from SPORTSDATA_AGENTS_DESK_DIR — the env var overrides --set)[/dim]")
    files = sorted(p.name for p in current.iterdir() if p.is_file()) if current.is_dir() else []
    if files:
        console.print(f"[dim]{len(files)} file(s): {', '.join(files[:10])}{' …' if len(files) > 10 else ''}[/dim]")
    else:
        console.print("[dim]empty — ask an agent to export a board or a report here.[/dim]")
    console.print("Change it with [bold]agents desk --set /path/you/open[/bold].")


@app.command()
def engines(
    action: str = typer.Argument("status", help="status | connect"),
    url: str = typer.Option("", help="Hosted pricing API base URL (connect)"),
    key: str = typer.Option("", help="API key (connect; never stored without --write)"),
    write: bool = typer.Option(False, help="Append the connection to .env after verifying"),
) -> None:
    """Pricing-engine connection: show status, or verify + wire a hosted key.

    The platform runs fully without an engine; connecting one unlocks model
    fair prices (value watches, consistency scans, engine predictions).
    """
    from sportsdata_agents.quant.engines import EngineUnavailable, RemoteEngineBackend, resolve_engine

    if action == "status":
        try:
            engine = resolve_engine()
        except (EngineUnavailable, ValueError) as exc:
            typer.echo(f"engine: unavailable ({exc})")
            raise typer.Exit(1) from exc
        if engine is None:
            typer.echo("engine: not configured (backend=none)")
            typer.echo("unlock model fair prices: `agents engines connect --url ... --key ...`"
                       " or SPORTSDATA_AGENTS_ENGINE_BACKEND=local with the engines package installed")
            return
        typer.echo(f"engine: {type(engine).__name__}")
        try:
            typer.echo(f"sports: {', '.join(engine.sports())}")  # remote: network I/O
        except (EngineUnavailable, ValueError) as exc:
            typer.echo(f"engine: unavailable ({exc})")
            raise typer.Exit(1) from exc
        return
    if action == "connect":
        if not url or not key:
            typer.echo("connect needs --url and --key")
            raise typer.Exit(2)
        try:
            sports = RemoteEngineBackend(url, key).sports()
        except EngineUnavailable as exc:
            typer.echo(f"verification failed: {exc}")
            raise typer.Exit(1) from exc
        typer.echo(f"verified: {len(sports)} sports at {url}")
        lines = [
            "SPORTSDATA_AGENTS_ENGINE_BACKEND=remote",
            f"SPORTSDATA_AGENTS_ENGINE_API_URL={url}",
            f"SPORTSDATA_AGENTS_ENGINE_API_KEY={key}",
        ]
        if write:
            from pathlib import Path

            env_path = Path(".env")
            existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
            kept = [ln for ln in existing.splitlines()
                    if not ln.startswith(("SPORTSDATA_AGENTS_ENGINE_BACKEND=",
                                          "SPORTSDATA_AGENTS_ENGINE_API_URL=",
                                          "SPORTSDATA_AGENTS_ENGINE_API_KEY="))]
            env_path.write_text("\n".join([*kept, *lines]) + "\n", encoding="utf-8")
            env_path.chmod(0o600)  # the key lives here — owner-only
            typer.echo(f"written to {env_path.resolve()} (key stored there only, mode 600)")
        else:
            masked = "…" + key[-4:] if len(key) > 8 else "…"  # last 4: prefixes are guessable
            typer.echo("add to your environment (key shown masked; re-run with --write to store):")
            typer.echo("  SPORTSDATA_AGENTS_ENGINE_BACKEND=remote")
            typer.echo(f"  SPORTSDATA_AGENTS_ENGINE_API_URL={url}")
            typer.echo(f"  SPORTSDATA_AGENTS_ENGINE_API_KEY={masked}")
        return
    typer.echo(f"unknown action {action!r} (use status | connect)")
    raise typer.Exit(2)


@app.command(name="update-data")
def update_data(
    url: str | None = typer.Option(None, "--url", help="Feed URL (default: SPORTSDATA_DATA_FEED_URL)."),
    check: bool = typer.Option(False, "--check", help="Just print the applied overlay version, don't fetch."),
) -> None:
    """Pull the latest signed data bundle (market dictionary, capability labels)
    and apply it as an overlay — refreshes the data plane between app releases.
    Verified offline against the baked SPORTSDATA_DATA_PUBKEY."""
    import os

    from rich.console import Console

    from sportsdata_agents.operations.datafeed import DataFeedError, applied_version, fetch_and_apply

    console = Console()
    if check:
        version = applied_version()
        console.print(f"data overlay: [cyan]{version or 'none (running packaged data)'}[/cyan]")
        return

    feed = url or os.environ.get("SPORTSDATA_DATA_FEED_URL")
    if not feed:
        console.print("[red]no feed URL[/red] — pass --url or set SPORTSDATA_DATA_FEED_URL")
        raise typer.Exit(1)
    try:
        result = fetch_and_apply(feed)
    except DataFeedError as e:
        console.print(f"[red]update failed:[/red] {e}")
        raise typer.Exit(1) from e
    except Exception as e:  # network/parse — actionable, not a stack trace
        console.print(f"[red]could not fetch the data feed:[/red] {e}")
        raise typer.Exit(1) from e
    applied = ", ".join(result["applied"]) or "nothing"
    console.print(f"[green]✓ applied[/green] {applied} (version {result['version']})")


@app.command(name="app")
def app_cmd(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host (localhost-only by default)."),
    port: int = typer.Option(8765, "--port"),
    no_conductor: bool = typer.Option(False, "--no-conductor",
                                      help="Gateway only — don't run ingest/monitor/custodian."),
) -> None:
    """The desktop daemon: gateway + the conductor loop (ingest/resolve/monitor/
    custodian) in ONE supervised process — no crontab, no .env. Ctrl-C to stop."""
    import logging

    from dotenv import load_dotenv

    from sportsdata_agents.app import run_app
    from sportsdata_agents.app.wizard import configured_provider

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if configured_provider() is None:
        from rich.console import Console

        Console().print("[yellow]No model key found — run [bold]agents setup[/bold] first.[/yellow]")
        raise typer.Exit(1)
    _require_entitlement("full_app")
    run_app(host=host, port=port, with_conductor=not no_conductor)


@app.command(name="desktop")
def desktop_cmd(
    port: int = typer.Option(8765, "--port", help="Preferred local port (auto-picks a free one if taken)."),
    no_conductor: bool = typer.Option(False, "--no-conductor",
                                      help="Gateway only — don't run ingest/monitor/custodian."),
) -> None:
    """The desktop app in its OWN native window (no web browser). Starts the
    gateway + conductor behind a native web view; closing the window quits."""
    import logging

    from dotenv import load_dotenv

    from sportsdata_agents.app.wizard import configured_provider

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if configured_provider() is None:
        from rich.console import Console

        Console().print("[yellow]No model key found — run [bold]agents setup[/bold] first.[/yellow]")
        raise typer.Exit(1)
    _require_entitlement("full_app")
    from sportsdata_agents.app.desktop import run_desktop

    run_desktop(port=port, with_conductor=not no_conductor)


@app.command()
def slack() -> None:
    """Run the Slack adapter (Socket Mode). Needs SLACK_BOT_TOKEN + SLACK_APP_TOKEN
    and the 'slack' add-on (Pro tier)."""
    from dotenv import load_dotenv

    load_dotenv()
    _require_addon("slack")
    from sportsdata_agents.interfaces.slack.app import serve_socket_mode

    serve_socket_mode()


@app.command()
def discord() -> None:
    """Run the Discord adapter. Needs DISCORD_BOT_TOKEN + a running gateway
    (`agents serve`). Install the extra: pip install 'sportsdata-agents[discord]'.
    Needs the 'discord' add-on (Pro tier)."""
    from dotenv import load_dotenv

    load_dotenv()
    _require_addon("discord")
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
        feeds_due_in_window,
        ingest_once,
        prune_snapshots,
        run_loop,
    )
    from sportsdata_agents.operations.ingestion.worker import INGEST_MAX_BYTES

    console = Console()
    settings = get_settings()
    from sportsdata_agents.operations.ingestion.worker import tuned_feeds

    # operator cadence overrides (priority sharps tier + explicit per-feed map)
    feeds = tuned_feeds() if feed is None else [next(f for f in tuned_feeds() if f.name == feed)]
    from sportsdata_agents.tools.ops import disabled_feeds as _disabled

    skip = _disabled()
    if skip:
        feeds = [f for f in feeds if f.name not in skip]
        console.print(f"[dim]skipping ops-disabled feeds: {', '.join(sorted(skip))}[/dim]")
    if pace is not None and feed is None:
        from sportsdata_agents.operations.ingestion.worker import paced_feeds

        feeds = paced_feeds(feeds, pace)
        console.print(f"[dim]proximity pace: hot-tier feeds floored to {pace}s[/dim]")
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
    # The ops plane is operator-only — this is THE path that injects GitHub/git creds and
    # remediation tools. The crypto gate was enforced in the scheduler + HTTP panel but NOT
    # here, so a release install could reach ops tools from the CLI. Gate it.
    from sportsdata_agents.operations.scheduler import is_operator

    if not is_operator():
        raise typer.BadParameter(
            "ops agents run only on the operator's deployment. "
            "This install can run product-plane agents with `agents run`."
        )

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

    from sportsdata_agents.operations.scheduler import is_operator

    if not is_operator():
        raise typer.BadParameter("ops commands run only on the operator's deployment.")

    console = Console()

    async def _run() -> None:
        from sportsdata_agents.config import get_settings
        from sportsdata_agents.data.db import make_engine, make_sessionmaker
        from sportsdata_agents.operations.health import run_health, summarise_health

        engine = make_engine(get_settings().database_url)
        try:
            health = await run_health(make_sessionmaker(engine))
            for line in summarise_health(health):
                console.print(line)
        finally:
            await engine.dispose()

    asyncio.run(_run())


@ops_app.command(name="budget-watch")
def ops_budget_watch() -> None:
    """Push an operator alert (Slack/Discord) if the period budget is breached.
    Rate-limited; the conductor runs this hourly. Enforcement is separate — the
    gateway already refuses calls over budget; this is just the heads-up."""
    import asyncio

    from dotenv import load_dotenv

    load_dotenv()

    from rich.console import Console

    from sportsdata_agents.operations.scheduler import is_operator

    if not is_operator():
        raise typer.BadParameter("ops commands run only on the operator's deployment.")

    console = Console()

    async def _run() -> None:
        from sportsdata_agents.config import get_settings
        from sportsdata_agents.data.db import make_engine, make_sessionmaker
        from sportsdata_agents.operations.budget_watch import push_budget_breach

        engine = make_engine(get_settings().database_url)
        try:
            res = await push_budget_breach(make_sessionmaker(engine))
        finally:
            await engine.dispose()
        if res.get("pushed"):
            console.print(f"[red]budget breach pushed[/red] — targets: {res.get('targets')}")
        else:
            console.print(f"[dim]no push: {res.get('reason')}[/dim]")

    asyncio.run(_run())


@ops_app.command(name="status")
def ops_status(
    limit: int = typer.Option(10, "--limit", help="How many recent ops runs to show."),
) -> None:
    """What the ops plane has been doing FOR YOU: recent ops-agent runs, open
    incidents/escalations, disabled feeds, and the scheduled-job status."""
    import asyncio

    from dotenv import load_dotenv

    load_dotenv()
    from rich.console import Console

    from sportsdata_agents.operations.scheduler import is_operator
    from sportsdata_agents.operations.scheduler import status as job_status
    from sportsdata_agents.tools.ops import read_ops_state

    console = Console()
    mode = "[green]ON[/green]" if is_operator() else "[yellow]off[/yellow] (ops jobs paused on this install)"
    console.print(f"[bold]Operator mode:[/bold] {mode}")

    state = read_ops_state()
    escalations = state.get("escalations") or []
    disabled = state.get("disabled_feeds") or []
    if escalations:
        console.print(f"\n[bold red]Open escalations ({len(escalations)}):[/bold red]")
        for e in escalations[-5:]:
            console.print(f"  • {e.get('summary', '?')} [dim]{e.get('at', '')}[/dim]")
    if disabled:
        console.print(f"\n[yellow]Disabled feeds:[/yellow] {', '.join(disabled)}")

    # recent ops-plane agent runs (tenant=platform)
    async def _runs() -> list[Any]:
        from sqlalchemy import desc, select

        from sportsdata_agents.config import get_settings
        from sportsdata_agents.data.db import make_engine, make_sessionmaker
        from sportsdata_agents.data.models import AgentRun

        engine = make_engine(get_settings().database_url)
        try:
            async with make_sessionmaker(engine)() as s:
                return list((await s.execute(
                    select(AgentRun).where(AgentRun.tenant_id == "platform")
                    .order_by(desc(AgentRun.created_at)).limit(limit)
                )).scalars().all())
        finally:
            await engine.dispose()

    try:
        runs = asyncio.run(_runs())
    except Exception as e:  # DB down → still show the rest
        runs = []
        console.print(f"[dim](recent runs unavailable: {e})[/dim]")
    if runs:
        console.print("\n[bold]Recent ops runs:[/bold]")
        for r in runs:
            mark = "[green]✓[/green]" if r.status == "ok" else ("[red]✗[/red]" if r.status == "error" else "·")
            when = r.created_at.strftime("%m-%d %H:%M") if r.created_at else "?"
            console.print(f"  {mark} {r.agent:18} ${float(r.cost_usd):.4f} [dim]{when}[/dim]")

    console.print("\n[bold]Scheduled jobs:[/bold]")
    for name, info in job_status().items():
        last = info.get("last_run") or "never"
        fails = info.get("consecutive_failures", 0)
        flag = f" [red]{fails} fails[/red]" if fails else ""
        console.print(f"  {name:18} [dim]{info.get('schedule', '')} · last {last}{flag}[/dim]")


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
def digest(
    push: bool = typer.Option(False, "--push", help="Broadcast to the operator channels."),
) -> None:
    """The morning summary: yesterday's alert P&L, today's racing volume, the
    freshest standing edges, and feed health — one push, then back to the
    real-time alerts. Disable the daily cron with SPORTSDATA_AGENTS_DIGEST=off."""
    import asyncio
    import os

    from dotenv import load_dotenv

    load_dotenv()
    if os.environ.get("SPORTSDATA_AGENTS_DIGEST", "").lower() in ("off", "0", "false"):
        typer.echo("digest disabled (SPORTSDATA_AGENTS_DIGEST=off)")
        return

    from rich.console import Console

    from sportsdata_agents.config import get_settings
    from sportsdata_agents.data.db import make_engine, make_sessionmaker
    from sportsdata_agents.observability.notify import operator_broadcast, slack_to_plain
    from sportsdata_agents.quant.scoreboard import alert_pnl

    console = Console()

    async def _run() -> None:
        import datetime as dt

        from sqlalchemy import func, select

        from sportsdata_agents.data.models import Alert, OddsSnapshot

        engine = make_engine(get_settings().database_url)
        sf = make_sessionmaker(engine)
        try:
            now = dt.datetime.now(dt.UTC)
            async with sf() as s:
                report = await alert_pnl(s, since=now - dt.timedelta(hours=24))
                alerts_24h = (await s.execute(
                    select(Alert.kind, func.count()).where(
                        Alert.created_at > now - dt.timedelta(hours=24)
                    ).group_by(Alert.kind))).all()
                races_today = (await s.execute(
                    select(func.count(func.distinct(OddsSnapshot.event_external_id))).where(
                        OddsSnapshot.market == "win",
                        OddsSnapshot.start_time > now,
                        OddsSnapshot.start_time < now + dt.timedelta(hours=18),
                    ))).scalar() or 0
                stale = (await s.execute(
                    select(OddsSnapshot.provider,
                           func.max(OddsSnapshot.captured_at).label("last"))
                    .group_by(OddsSnapshot.provider))).all()
            racing = report["racing"]
            lines = [":newspaper: Morning digest"]
            fired: dict[str, int] = {str(k): int(c) for (k, c) in alerts_24h}
            total = sum(fired.values())
            lines.append(f"Last 24h: {total} alerts"
                         + (f" ({', '.join(f'{k} {c}' for k, c in sorted(fired.items()))})"
                            if fired else ""))
            if racing["settled"]:
                lines.append(f"Racing P&L: {racing['wins']}/{racing['settled']} won, "
                             f"${racing['pnl']:+.2f} on ${racing['staked']:.2f} staked")
            value = report.get("value") or {}
            if value.get("settled"):
                lines.append(f"Value P&L: {value['wins']}/{value['settled']} won, "
                             f"${value['pnl']:+.2f}")
            lines.append(f"Today: {races_today} races captured and upcoming")
            lagging = [prov for prov, last in stale
                       if last and (now - (last if last.tzinfo else last.replace(tzinfo=dt.UTC))
                                    ).total_seconds() > 1800]
            lines.append("Feeds: all fresh" if not lagging
                         else f"Feeds lagging >30m: {', '.join(sorted(lagging))}")
            for tip in report.get("suggestions") or []:
                lines.append(f":wrench: {tip}")
            text = "\n".join(lines)
            console.print(slack_to_plain(text))
            if push:
                await operator_broadcast(text)
        finally:
            await engine.dispose()

    asyncio.run(_run())


@app.command()
def scoreboard(
    days: int = typer.Option(7, "--days", help="Window to grade (default: the last week)."),
    push: bool = typer.Option(False, "--push", help="Broadcast the report to the operator "
                                                    "channels (ntfy/Slack/Discord) as well."),
) -> None:
    """Alert P&L: grade every alert's PRINTED Kelly stake against recorded
    results — racing settles fully; arbs report locked profit when still
    takeable; other value kinds join the P&L with Phase B."""
    import asyncio

    from dotenv import load_dotenv

    load_dotenv()

    from rich.console import Console

    from sportsdata_agents.config import get_settings
    from sportsdata_agents.data.db import make_engine, make_sessionmaker
    from sportsdata_agents.observability.notify import operator_broadcast, slack_to_plain
    from sportsdata_agents.quant.scoreboard import alert_pnl, format_scoreboard

    console = Console()

    async def _run() -> None:
        import datetime as dt

        engine = make_engine(get_settings().database_url)
        sf = make_sessionmaker(engine)
        try:
            async with sf() as session:
                report = await alert_pnl(
                    session, since=dt.datetime.now(dt.UTC) - dt.timedelta(days=days))
            text = format_scoreboard(report)
            console.print(slack_to_plain(text))
            if push:
                await operator_broadcast(text)
        finally:
            await engine.dispose()

    asyncio.run(_run())


@app.command()
def monitor(
    watch: str | None = typer.Option(None, "--add", help='Create a watch inline: "name:kind:threshold" '
                                                         '(e.g. "big-moves:line_move:8").'),
    channel: str = typer.Option("log", "--channel", help='Push target for --add: Slack channel id, '
                                                         '"discord[:ENV_VAR]" (webhook), '
                                                         '"ntfy[:ENV_VAR]" (phone push), or "log".'),
) -> None:
    """Run one monitoring pass: every active watch scans the price stream since its
    cursor and fires push alerts (M3.2). Deterministic — no LLM. The conductor
    (`agents schedule`) runs this every 5 minutes."""
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
        from sportsdata_agents.data.db import ensure_schema, make_engine, make_sessionmaker

        engine = make_engine(get_settings().database_url)
        try:
            # the conductor is the one process guaranteed to run before every
            # job, so it owns keeping the SQLite warehouse's schema current —
            # without this, a model that grows a column strands every cron job
            # on OperationalError (lived: resolve failed 1391 consecutive runs
            # on a missing odds_snapshots.end_time)
            await ensure_schema(engine)
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
def custodian(
    force_days: int | None = typer.Option(None, "--prune-days",
                                          help="Override the ladder: prune to N days now."),
) -> None:
    """The data custodian: adaptive disk-aware retention (hold when space is
    plentiful; backup+prune as it tightens). The conductor runs this hourly.
    Deterministic — no LLM decides what data dies."""
    import asyncio

    from dotenv import load_dotenv

    load_dotenv()
    from rich.console import Console

    from sportsdata_agents.config import get_settings
    from sportsdata_agents.operations.retention import run_custodian

    console = Console()
    report = asyncio.run(run_custodian(get_settings().database_url, force_days=force_days))
    console.print(f"✓ custodian: {report}")


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
def form() -> None:
    """Capture official race form (barriers, weights, jockeys, past starts) via
    TAB's authenticated tier into race_form — the racing ratings' real inputs.
    Needs TAB_CLIENT_ID/TAB_CLIENT_SECRET; cron half-hourly during racing."""
    import asyncio

    from dotenv import load_dotenv

    load_dotenv()

    from rich.console import Console

    from sportsdata_agents.config import get_settings
    from sportsdata_agents.data.base import Base
    from sportsdata_agents.data.db import make_engine, make_sessionmaker
    from sportsdata_agents.mcp.manager import MCPManager
    from sportsdata_agents.operations.ingestion.form import ingest_tab_form
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
                groups=["tab.racing", "sportsbet.racing"], command=settings.mcp_command,
                extra_env={"SPORTSDATA_MCP_MAX_BYTES": str(INGEST_MAX_BYTES)},
            ) as manager:
                report = await ingest_tab_form(manager, sf)
                from sportsdata_agents.operations.ingestion.form import ingest_sportsbet_form

                sb_report = await ingest_sportsbet_form(manager, sf)
                report = {"tab": report, "sportsbet": sb_report}
            console.print(f"✓ form: {report}")
        finally:
            await engine.dispose()

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
        ingest_prediction_resolutions,
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
                        "nrl.public.core", "mlb.schedule", "espn.scores",
                        "kalshi.markets", "polymarket.gamma"],
                command=settings.mcp_command,
                extra_env={"SPORTSDATA_MCP_MAX_BYTES": str(INGEST_MAX_BYTES)},
            ) as manager:
                racing = await ingest_racing_results(manager, sf)
                league = await ingest_league_results(manager, sf)
                predictions = await ingest_prediction_resolutions(manager, sf)
            console.print(f"✓ racing: {racing} settled")
            console.print(f"✓ leagues: {league}")
            console.print(f"✓ prediction markets: {predictions}")
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


@app.command(name="price-slate")
def price_slate(
    anchor_minutes: float = typer.Option(45.0, "--anchor-minutes",
                                         help="Scan events whose h2h/total anchors moved this recently."),
    dedupe_hours: float = typer.Option(12.0, "--dedupe-hours",
                                       help="At most one recording per (book, event) in this window."),
    max_events: int = typer.Option(80, "--max-events", help="Board-pricing cap per run."),
) -> None:
    """Record engine fair prices for the upcoming slate (the measurement half
    of the value loop: model_value alerts price boards inline, THIS persists
    them as predictions so backtest/CLV can grade the engine later)."""
    import asyncio
    import datetime as _dt

    from dotenv import load_dotenv

    load_dotenv()

    from rich.console import Console

    from sportsdata_agents.config import get_settings
    from sportsdata_agents.data.base import Base
    from sportsdata_agents.data.db import make_engine, make_sessionmaker
    from sportsdata_agents.data.repository import TenantScope
    from sportsdata_agents.quant.slate import record_slate

    console = Console()

    async def _run() -> None:
        settings = get_settings()
        engine = make_engine(settings.database_url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        from sportsdata_agents.quant.ratings import record_ratings_slate

        try:
            scope = TenantScope(settings.default_tenant, settings.default_workspace)
            now = _dt.datetime.now(_dt.UTC)
            async with make_sessionmaker(engine)() as session:
                report = await record_slate(
                    session, scope, now=now, anchor_minutes=anchor_minutes,
                    dedupe_hours=dedupe_hours, max_events=max_events,
                )
            # the book-independent fair prices ride the same job: ratings from
            # results, form from the TAB capture — its own session so a locked
            # anchored pass never poisons this one
            async with make_sessionmaker(engine)() as session:
                ratings_report = await record_ratings_slate(
                    session, scope, now=now,
                    dedupe_hours=dedupe_hours, max_events=max_events,
                )
        finally:
            await engine.dispose()
        if report.get("error"):
            console.print(f"[yellow]slate: {report['error']}[/yellow]")
        else:
            console.print(f"recorded={report['recorded']} events={report['events']} "
                          f"deduped={report['skipped_dedupe']} unseedable={report['skipped_unseedable']}")
        if ratings_report.get("error"):
            console.print(f"[yellow]ratings: {ratings_report['error']}[/yellow]")
        else:
            console.print(f"ratings: recorded={ratings_report['recorded']} "
                          f"events={ratings_report['events']} "
                          f"deduped={ratings_report['skipped_dedupe']} "
                          f"unrated={ratings_report['skipped_unrated']}")

    asyncio.run(_run())


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


# ── watches: the notification-preference console ────────────────────────────
# Every alert kind's knobs live in operations.watch_registry; these commands
# make them USER-editable without touching SQL or JSON by hand.

watches_app = typer.Typer(
    name="watches",
    help="Your alert watches: list them, tune any knob (`set name key=value`), "
         "add/enable/disable/remove, and `kinds` documents every parameter.",
    no_args_is_help=True,
)
app.add_typer(watches_app, name="watches")


def _watches_session():
    from dotenv import load_dotenv

    load_dotenv()

    from sportsdata_agents.config import get_settings
    from sportsdata_agents.data.db import make_engine, make_sessionmaker

    engine = make_engine(get_settings().database_url)
    return engine, make_sessionmaker(engine)


async def _watch_by_name(session: Any, name: str) -> Any:
    from sqlalchemy import select

    from sportsdata_agents.data.models import Subscription

    sub = (await session.execute(
        select(Subscription).where(Subscription.name == name))).scalars().first()
    if sub is None:
        typer.echo(f"error: no watch named {name!r} — see `agents watches list`", err=True)
        raise typer.Exit(1)
    return sub


def _parse_kv(pairs: list[str]) -> dict[str, Any]:
    from sportsdata_agents.operations.watch_registry import parse_value

    updates: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            typer.echo(f"error: expected key=value, got {pair!r}", err=True)
            raise typer.Exit(1)
        key, raw = pair.split("=", 1)
        updates[key.strip()] = parse_value(raw)
    return updates


@watches_app.command(name="list")
def watches_list() -> None:
    """Every watch: kind, channel, on/off, custom knobs, and recent activity."""
    import asyncio
    import datetime as _dt

    from rich.console import Console
    from rich.table import Table

    async def _run() -> None:
        from sqlalchemy import func, select

        from sportsdata_agents.data.models import Alert, Subscription

        engine, sf = _watches_session()
        try:
            async with sf() as session:
                subs = (await session.execute(
                    select(Subscription).order_by(Subscription.kind, Subscription.name)
                )).scalars().all()
                table = Table(title="alert watches")
                for col in ("name", "kind", "channel", "on", "custom params", "7d", "last fired"):
                    table.add_column(col)
                week_ago = _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=7)
                for sub in subs:
                    fired = (await session.execute(
                        select(func.count(Alert.id), func.max(Alert.created_at))
                        .where(Alert.subscription_id == sub.id, Alert.created_at >= week_ago)
                    )).one()
                    custom = ", ".join(f"{k}={v}" for k, v in sorted((sub.params or {}).items()))
                    table.add_row(
                        sub.name, sub.kind, sub.channel or "log",
                        "✓" if sub.active else "✗",
                        custom or "[dim](defaults)[/dim]",
                        str(fired[0] or 0),
                        str(fired[1] or "-"),
                    )
                Console().print(table)
        finally:
            await engine.dispose()

    asyncio.run(_run())


@watches_app.command(name="kinds")
def watches_kinds(
    kind: str | None = typer.Argument(None, help="One kind for full detail; omit for the overview."),
) -> None:
    """Every watch kind and every knob it honours — defaults and meanings."""
    from rich.console import Console
    from rich.table import Table

    from sportsdata_agents.operations.watch_registry import (
        COMMON_PARAMS,
        WATCH_PARAMS,
        params_for,
    )

    console = Console()
    if kind:
        if kind not in WATCH_PARAMS:
            typer.echo(f"error: unknown kind {kind!r} — kinds: {', '.join(sorted(WATCH_PARAMS))}",
                       err=True)
            raise typer.Exit(1)
        table = Table(title=f"{kind} — every knob (set with: agents watches set NAME key=value)")
        for col in ("param", "default", "what it does"):
            table.add_column(col)
        for name, (default, help_text) in params_for(kind).items():
            table.add_row(name, repr(default), help_text)
        console.print(table)
        return
    table = Table(title="watch kinds (agents watches kinds KIND for full detail)")
    for col in ("kind", "its knobs"):
        table.add_column(col)
    for kind_name, params in sorted(WATCH_PARAMS.items()):
        table.add_row(kind_name, ", ".join(sorted(params)))
    console.print(table)
    console.print(f"[dim]every kind also honours: {', '.join(sorted(COMMON_PARAMS))}[/dim]")


@watches_app.command(name="add")
def watches_add(
    name: str = typer.Argument(..., help="A name for the watch (unique)."),
    kind: str = typer.Argument(..., help="A watch kind — see `agents watches kinds`."),
    params: list[str] = typer.Argument(None, help="Any key=value knobs (validated against the kind)."),
    channel: str = typer.Option("ntfy", "--channel", help='Push target: "ntfy[:ENV]", Slack channel id, '
                                                          '"discord[:ENV]", or "log".'),
) -> None:
    """Create a watch. Knobs you don't set use the kind's defaults."""
    import asyncio

    from sportsdata_agents.operations.watch_registry import validate_params

    updates = _parse_kv(params or [])
    problems = validate_params(kind, updates)
    if problems:
        for problem in problems:
            typer.echo(f"error: {problem}", err=True)
        raise typer.Exit(1)

    async def _run() -> None:
        from sportsdata_agents.data.base import Base
        from sportsdata_agents.data.models import Subscription

        engine, sf = _watches_session()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        try:
            async with sf() as session:
                session.add(Subscription(tenant_id="local", workspace_id="local",
                                         name=name, kind=kind, channel=channel,
                                         params=updates))
                await session.commit()
        finally:
            await engine.dispose()
        typer.echo(f"✓ watch {name!r} ({kind}, channel={channel}"
                   + (f", {updates}" if updates else "") + ")")

    asyncio.run(_run())


@watches_app.command(name="set")
def watches_set(
    name: str = typer.Argument(..., help="The watch to change."),
    params: list[str] = typer.Argument(..., help="key=value knobs; channel=... and active=... "
                                                 "work too; key=null resets a knob to its default."),
) -> None:
    """Tune a watch. Example: agents watches set racing-value min_edge_pct=10 quiet_hours=23-08."""
    import asyncio

    from sportsdata_agents.operations.watch_registry import validate_params

    updates = _parse_kv(params)

    async def _run() -> None:
        engine, sf = _watches_session()
        try:
            async with sf() as session:
                sub = await _watch_by_name(session, name)
                if "channel" in updates:
                    sub.channel = str(updates.pop("channel"))
                if "active" in updates:
                    sub.active = bool(updates.pop("active"))
                problems = validate_params(sub.kind, updates)
                if problems:
                    for problem in problems:
                        typer.echo(f"error: {problem}", err=True)
                    raise typer.Exit(1)
                merged = dict(sub.params or {})
                for key, value in updates.items():
                    if value is None:
                        merged.pop(key, None)  # back to the kind's default
                    else:
                        merged[key] = value
                sub.params = merged
                await session.commit()
                custom = ", ".join(f"{k}={v}" for k, v in sorted(merged.items())) or "(defaults)"
                typer.echo(f"✓ {sub.name} [{sub.kind}] channel={sub.channel} "
                           f"active={bool(sub.active)} — {custom}")
        finally:
            await engine.dispose()

    asyncio.run(_run())


def _watches_toggle(name: str, active: bool) -> None:
    import asyncio

    async def _run() -> None:
        engine, sf = _watches_session()
        try:
            async with sf() as session:
                sub = await _watch_by_name(session, name)
                sub.active = active
                await session.commit()
                typer.echo(f"✓ {name} {'enabled' if active else 'disabled'}")
        finally:
            await engine.dispose()

    asyncio.run(_run())


@watches_app.command(name="enable")
def watches_enable(name: str = typer.Argument(..., help="The watch to turn on.")) -> None:
    """Turn a watch on."""
    _watches_toggle(name, True)


@watches_app.command(name="disable")
def watches_disable(name: str = typer.Argument(..., help="The watch to turn off.")) -> None:
    """Turn a watch off (kept, not deleted — re-enable any time)."""
    _watches_toggle(name, False)


@watches_app.command(name="rm")
def watches_rm(name: str = typer.Argument(..., help="The watch to delete.")) -> None:
    """Delete a watch (its past alerts stay in the record)."""
    import asyncio

    async def _run() -> None:
        engine, sf = _watches_session()
        try:
            async with sf() as session:
                sub = await _watch_by_name(session, name)
                await session.delete(sub)
                await session.commit()
                typer.echo(f"✓ watch {name!r} removed")
        finally:
            await engine.dispose()

    asyncio.run(_run())
