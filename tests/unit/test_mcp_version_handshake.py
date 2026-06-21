"""The agents read the data plane's version from the MCP initialize handshake and warn on a
too-old MCP (the two repos version independently outside the co-bundled DMG)."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from sportsdata_agents.mcp.manager import MIN_MCP_VERSION, _check_mcp_version, _ver_tuple

pytestmark = pytest.mark.unit


def _init(name: str, version: str):
    return SimpleNamespace(serverInfo=SimpleNamespace(name=name, version=version))


def test_ver_tuple_parses_semver():
    assert _ver_tuple("0.12.4") == (0, 12, 4)
    assert _ver_tuple("v1.2") == (1, 2)
    assert _ver_tuple("") == ()


def test_warns_on_too_old_mcp(caplog):
    old = ".".join(str(x) for x in (MIN_MCP_VERSION[0], max(MIN_MCP_VERSION[1] - 1, 0), 0))
    with caplog.at_level(logging.WARNING):
        _check_mcp_version(_init("sportsdata-mcp", old))
    assert any("older than the minimum" in r.message for r in caplog.records)


def test_silent_on_current_or_foreign_server(caplog):
    with caplog.at_level(logging.WARNING):
        _check_mcp_version(_init("sportsdata-mcp", "9.9.9"))     # new enough
        _check_mcp_version(_init("some-other-mcp", "0.0.1"))      # not ours
        _check_mcp_version(_init("sportsdata-mcp", ""))           # no version reported
    assert not caplog.records
