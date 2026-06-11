"""M3.5 (D27) — spec versioning: archives, workspace pins, deprecation, schema guard.

Exit gate: bump a module version without breaking a workspace pinned to the old
one; migration (re-pinning) applies on opt-in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sportsdata_agents.agents.loader import (
    SpecError,
    load_spec_catalog,
    load_spec_text,
    load_specs_dir,
    resolve_pins,
)

pytestmark = pytest.mark.unit


def _write(directory: Path, name: str, agent_id: str, version: str, prompt: str,
           deprecated: str | None = None) -> None:
    dep = f"\n  deprecated: \"{deprecated}\"" if deprecated else ""
    (directory / name).write_text(f"""
spec_version: 1
agent:
  id: {agent_id}
  display_name: X
  version: {version}{dep}
  system_prompt: {prompt}
""")


def test_version_bump_does_not_break_a_pinned_workspace(tmp_path: Path) -> None:
    """The exit gate, in miniature: v0.2.0 ships as latest, v0.1.0 stays archived;
    an unpinned workspace gets the new one, a pinned workspace keeps the old one,
    and re-pinning IS the opt-in migration."""
    _write(tmp_path, "scout.yaml", "scout", "0.2.0", "the NEW prompt")
    _write(tmp_path, "scout@0.1.0.yaml", "scout", "0.1.0", "the OLD prompt",
           deprecated="superseded by 0.2.0; archive removed after 2026-09-01")

    latest = load_specs_dir(tmp_path)
    assert latest["scout"].version == "0.2.0"  # archives never shadow latest

    catalog = load_spec_catalog(tmp_path)
    assert sorted(catalog["scout"]) == ["0.1.0", "0.2.0"]

    unpinned = resolve_pins(catalog, latest, {})
    assert unpinned["scout"].system_prompt.strip() == "the NEW prompt"

    pinned = resolve_pins(catalog, latest, {"scout": "0.1.0"})
    assert pinned["scout"].system_prompt.strip() == "the OLD prompt"
    assert pinned["scout"].deprecated  # still loads — pinned workspaces must not break

    migrated = resolve_pins(catalog, latest, {"scout": "0.2.0"})  # opt-in migration
    assert migrated["scout"].version == "0.2.0"


def test_unknown_pin_fails_loudly(tmp_path: Path) -> None:
    _write(tmp_path, "scout.yaml", "scout", "0.2.0", "x")
    catalog = load_spec_catalog(tmp_path)
    with pytest.raises(SpecError, match="archive missing"):
        resolve_pins(catalog, load_specs_dir(tmp_path), {"scout": "0.0.9"})


def test_archive_filename_must_match_contents(tmp_path: Path) -> None:
    _write(tmp_path, "scout.yaml", "scout", "0.2.0", "x")
    _write(tmp_path, "scout@0.1.0.yaml", "scout", "0.3.0", "liar")  # version mismatch
    with pytest.raises(SpecError, match="archive name says"):
        load_spec_catalog(tmp_path)


def test_future_schema_version_is_refused() -> None:
    with pytest.raises(SpecError, match="not supported by this runtime"):
        load_spec_text("""
spec_version: 2
agent:
  id: x
  display_name: X
  system_prompt: y
""")


def test_workspace_pins_resolve_in_team_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TeamSession applies workspace.agent_versions against the builtin catalog."""
    from sportsdata_agents.gateway.service import TeamSession
    from sportsdata_agents.workspace import Workspace

    # no pins: loads fine (smoke); a bogus pin fails loudly at construction
    TeamSession(workspace=Workspace())
    with pytest.raises(SpecError, match="archive missing"):
        TeamSession(workspace=Workspace(agent_versions={"value_scout": "0.0.1"}))
