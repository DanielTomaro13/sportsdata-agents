"""`python -m sportsdata_agents` and the packaged-app entry point.

The PyInstaller bundle launches this; it forwards to the Typer CLI so the
desktop binary behaves exactly like `agents` (e.g. `sportsdata app`,
`sportsdata setup`, `sportsdata license`)."""

from sportsdata_agents.interfaces.cli.__main__ import app

if __name__ == "__main__":
    app()
