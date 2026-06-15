"""`python -m sportsdata_agents` and the packaged-app entry point.

The PyInstaller bundle launches this; it forwards to the Typer CLI so the
desktop binary behaves exactly like `agents` (e.g. `sportsdata app`,
`sportsdata setup`, `sportsdata license`)."""

import os
import sys

# PyInstaller strips .py source from the frozen bundle, but logfire's pydantic
# plugin calls inspect.getsource() when pydantic builds a schema validator →
# OSError "could not get source code", which crashes the desktop app on the
# first model. Pydantic plugins are pure instrumentation (they don't change
# validation), so disable them in the frozen binary. This MUST run before any
# pydantic model is imported, i.e. before the CLI import below.
if getattr(sys, "frozen", False):
    os.environ.setdefault("PYDANTIC_DISABLE_PLUGINS", "__all__")

from sportsdata_agents.interfaces.cli.__main__ import app

if __name__ == "__main__":
    app()
