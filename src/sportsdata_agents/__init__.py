"""sportsdata-agents — the agent plane over the sportsdata-mcp tool catalogue.

Advisory only: no agent ever places a bet or moves money. See docs/ARCHITECTURE.md.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:
    # Single source of truth is pyproject.toml. Hardcoding it here let `agents
    # version` drift 30 releases behind (it still reported 0.77.0 at 0.79.29).
    __version__ = _version("sportsdata-agents")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]
