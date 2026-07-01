"""Workbench per-agent model overrides — the user's pin of which model an agent uses.

Persisted in the gateway data dir (like ``mcp/prefs.py``), written by the workbench
Agents pane and read by the harness at run start. A pin WINS over the orchestrator's
per-run complexity pick — that's what "pin" means — and falls back to the spec's own
``model_tier`` when absent. Budgets clamp regardless of the model chosen, so a pin can
raise quality but never spend past a ceiling.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_FILE = "agent-prefs.json"


def _path():
    from sportsdata_agents.paths import data_dir

    return data_dir() / _FILE


def _valid_tier(value: str) -> bool:
    """Same shape rule as AgentSpec.model_tier: a known tier name, or an explicit
    provider-qualified model ("anthropic/claude-...")."""
    from sportsdata_agents.models.policy import TIERS

    return value in TIERS or "/" in value or ":" in value


def load_overrides() -> dict[str, str]:
    """agent_id → pinned tier/model (empty on any read/parse error)."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    vals = data.get("model_overrides") if isinstance(data, dict) else None
    if not isinstance(vals, dict):
        return {}
    return {str(k): str(v) for k, v in vals.items() if isinstance(v, str) and v}


def set_override(agent_id: str, tier: str | None) -> dict[str, str]:
    """Pin (or clear, with ``None``/``""``) an agent's model, persist, return the map."""
    cur = load_overrides()
    if tier:
        cur[agent_id] = tier
    else:
        cur.pop(agent_id, None)
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"model_overrides": dict(sorted(cur.items()))}, indent=2), encoding="utf-8")
    except OSError as e:  # best-effort — a write failure must not break the route
        logger.warning("could not persist agent model prefs: %s", e)
    return cur


def override_for(agent_id: str) -> str | None:
    """The pinned tier/model for ``agent_id``, or None. A hand-edited file with an
    invalid value is ignored (None) rather than crashing the run at model-call time."""
    value = load_overrides().get(agent_id)
    if value and _valid_tier(value):
        return value
    return None
