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


if __name__ == "__main__":  # pragma: no cover
    app()
