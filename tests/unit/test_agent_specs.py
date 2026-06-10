"""M0.6 — agent-spec schema, loader, lint, and CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from sportsdata_agents.agents.loader import (
    SpecError,
    lint_specs,
    load_builtin_specs,
    load_spec_text,
    load_specs_dir,
)
from sportsdata_agents.interfaces.cli.__main__ import app

pytestmark = pytest.mark.unit

VALID = """
spec_version: 1
agent:
  id: my_agent
  display_name: "My Agent"
  system_prompt: "Do the thing."
  tools:
    mcp_capabilities: [sport.prices]
    native: [vig_removal]
"""


# ── registration: the bundled specs ──────────────────────────────────────


def test_builtin_specs_load_and_register() -> None:
    specs = load_builtin_specs()
    assert {"orchestrator", "odds_specialist", "stats_specialist"} <= set(specs)
    assert lint_specs(specs) == []
    # the orchestrator's delegation targets all exist
    orch = specs["orchestrator"]
    assert set(orch.can_delegate_to) <= set(specs)
    # specialists carry capabilities, never raw money tools
    assert specs["odds_specialist"].tools.mcp_capabilities
    assert specs["stats_specialist"].tools.mcp_capabilities


def test_valid_spec_parses_with_defaults() -> None:
    spec = load_spec_text(VALID)
    assert spec.id == "my_agent"
    assert spec.model_tier == "balanced"  # default
    assert spec.context.retrieval == "jit" and spec.context.verify is True
    assert spec.limits.max_steps == 40
    assert spec.version == "0.1.0"


# ── malformed specs fail loudly, with the source in the error ────────────


@pytest.mark.parametrize(
    ("bad", "match"),
    [
        (VALID.replace("id: my_agent", "id: My-Agent"), "must match"),  # bad id shape
        (VALID + "  version: v1\n", "semver"),  # bad version
        (VALID + "  model_tier: ultra\n", "model_tier"),  # unknown tier
        (VALID + "  bogus_field: 1\n", "bogus_field"),  # unknown field rejected
        (VALID.replace("[sport.prices]", "[Sport Prices]"), "mcp capability"),  # cap shape
        (VALID.replace("[sport.prices]", "[sport.prices, sport.prices]"), "duplicate"),  # dup entries
        (VALID + "  skills: [Bad-Skill]\n", "skill"),  # skill shape
    ],
)
def test_malformed_specs_fail(bad: str, match: str) -> None:
    with pytest.raises(SpecError, match=match):
        load_spec_text(bad, source="bad.yaml")


def test_error_carries_source_path() -> None:
    with pytest.raises(SpecError, match=r"bad\.yaml"):
        load_spec_text("spec_version: 1\nagent: {}", source="bad.yaml")


def test_money_tool_in_spec_is_rejected() -> None:
    """The no-money invariant holds at authoring time (§13)."""
    bad = VALID.replace("native: [vig_removal]", "native: [place_bet]")
    with pytest.raises(SpecError, match="no-money"):
        load_spec_text(bad, source="bad.yaml")


def test_allowed_and_forbidden_overlap_rejected() -> None:
    bad = VALID + "  forbidden_capabilities: [sport.prices]\n"
    with pytest.raises(SpecError, match="both allowed and forbidden"):
        load_spec_text(bad, source="bad.yaml")


# ── directory loading + cross-spec lint ──────────────────────────────────


def _write(d: Path, name: str, text: str) -> None:
    (d / name).write_text(text, encoding="utf-8")


def test_duplicate_ids_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "a.yaml", VALID)
    _write(tmp_path, "b.yaml", VALID)
    with pytest.raises(SpecError, match="duplicate agent id"):
        load_specs_dir(tmp_path)


def test_underscore_files_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "_schema.yaml", "not: [valid")  # broken on purpose — must be ignored
    _write(tmp_path, "a.yaml", VALID)
    assert set(load_specs_dir(tmp_path)) == {"my_agent"}


def test_lint_catches_dangling_and_self_delegation(tmp_path: Path) -> None:
    _write(tmp_path, "a.yaml", VALID + "  can_delegate_to: [ghost, my_agent]\n")
    specs = load_specs_dir(tmp_path)
    problems = lint_specs(specs)
    assert any("ghost" in p for p in problems)
    assert any("delegate to itself" in p for p in problems)


def test_lint_catches_delegation_cycle(tmp_path: Path) -> None:
    a = VALID.replace("id: my_agent", "id: agent_a") + "  can_delegate_to: [agent_b]\n"
    b = VALID.replace("id: my_agent", "id: agent_b") + "  can_delegate_to: [agent_a]\n"
    _write(tmp_path, "a.yaml", a)
    _write(tmp_path, "b.yaml", b)
    problems = lint_specs(load_specs_dir(tmp_path))
    assert any("delegation cycle" in p for p in problems)


def test_yml_extension_also_loads(tmp_path: Path) -> None:
    _write(tmp_path, "a.yml", VALID)
    assert set(load_specs_dir(tmp_path)) == {"my_agent"}


def test_nonexistent_dir_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="does not exist"):
        load_specs_dir(tmp_path / "nope")


# ── CLI ──────────────────────────────────────────────────────────────────


def test_cli_lint_builtin_ok() -> None:
    result = CliRunner().invoke(app, ["lint"])
    assert result.exit_code == 0
    assert "lint passed" in result.output


def test_cli_lint_bad_dir_fails(tmp_path: Path) -> None:
    _write(tmp_path, "a.yaml", VALID + "  can_delegate_to: [ghost]\n")
    result = CliRunner().invoke(app, ["lint", "--dir", str(tmp_path)])
    assert result.exit_code == 1


def test_cli_lint_never_passes_on_wrong_or_empty_path(tmp_path: Path) -> None:
    """A typo'd path or empty dir must FAIL lint, not '✓ pass with 0 specs'."""
    missing = CliRunner().invoke(app, ["lint", "--dir", str(tmp_path / "typo")])
    assert missing.exit_code == 1
    empty = CliRunner().invoke(app, ["lint", "--dir", str(tmp_path)])
    assert empty.exit_code == 1


def test_cli_list_shows_agents() -> None:
    result = CliRunner().invoke(app, ["list"])
    assert result.exit_code == 0
    assert "orchestrator" in result.output
    assert "odds_specialist" in result.output
