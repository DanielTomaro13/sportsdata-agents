"""Workbench MCP preferences — which data providers the operator has globally turned OFF.

Persisted in the gateway data dir, written by the workbench toggle (gateway routes) and
read by the agent runtime, which passes the set to each MCP subprocess as
``SPORTSDATA_MCP_DISABLED_PROVIDERS`` so a disabled provider's tools never register. A
global narrow-only switch — it can never widen past what the licence already grants.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_FILE = "mcp-prefs.json"


def _path():
    from sportsdata_agents.paths import data_dir

    return data_dir() / _FILE


def load_disabled() -> set[str]:
    """The set of globally-disabled provider ids (empty on any read/parse error)."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    vals = data.get("disabled_providers") if isinstance(data, dict) else None
    return {str(p) for p in vals} if isinstance(vals, list) else set()


def set_disabled(provider: str, disabled: bool) -> set[str]:
    """Add/remove ``provider`` from the disabled set, persist, and return the new set."""
    cur = load_disabled()
    if disabled:
        cur.add(provider)
    else:
        cur.discard(provider)
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"disabled_providers": sorted(cur)}, indent=2), encoding="utf-8")
    except OSError as e:  # best-effort — a write failure must not break the toggle call
        logger.warning("could not persist MCP prefs: %s", e)
    return cur


def disabled_env() -> str:
    """The value for ``SPORTSDATA_MCP_DISABLED_PROVIDERS`` (comma-separated), or ``''``."""
    return ",".join(sorted(load_disabled()))
