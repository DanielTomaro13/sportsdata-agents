"""M0.1 smoke tests — prove the package imports and the scaffold is wired."""

from __future__ import annotations

import importlib

import pytest

import sportsdata_agents


@pytest.mark.unit
def test_version_is_a_string() -> None:
    assert isinstance(sportsdata_agents.__version__, str)
    assert sportsdata_agents.__version__.count(".") >= 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "module",
    [
        "sportsdata_agents.gateway",
        "sportsdata_agents.orchestrator",
        "sportsdata_agents.agents",
        "sportsdata_agents.mcp",
        "sportsdata_agents.tools",
        "sportsdata_agents.sandboxes",
        "sportsdata_agents.data",
        "sportsdata_agents.models",
        "sportsdata_agents.observability",
        "sportsdata_agents.operations",
        "sportsdata_agents.eval",
        "sportsdata_agents.interfaces",
    ],
)
def test_subpackages_import(module: str) -> None:
    assert importlib.import_module(module) is not None
