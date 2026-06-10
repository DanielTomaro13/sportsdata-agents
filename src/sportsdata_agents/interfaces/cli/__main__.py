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


if __name__ == "__main__":  # pragma: no cover
    app()
