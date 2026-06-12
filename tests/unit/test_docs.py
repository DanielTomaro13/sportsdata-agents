"""Documentation lives and the docs_keeper agent that maintains it is PR-only."""

from __future__ import annotations

from pathlib import Path

import pytest

from sportsdata_agents.agents.loader import load_builtin_specs

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]


def test_core_docs_exist() -> None:
    for name in ("ARCHITECTURE", "STRUCTURE", "AGENTS", "UPDATING", "NEXT_STEPS"):
        assert (_ROOT / "docs" / f"{name}.md").is_file(), f"docs/{name}.md missing"


def test_docs_keeper_is_ops_and_pr_only() -> None:
    dk = load_builtin_specs()["docs_keeper"]
    assert dk.plane == "ops"  # maintenance agent, never licence-gated
    # it proposes (CI-gated PR) but cannot merge, and holds no product data tools
    assert "propose_change" in dk.tools.native
    assert not dk.tools.mcp_capabilities
