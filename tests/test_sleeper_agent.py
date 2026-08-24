"""Sleeper: the id→name cache, and the fact that this agent can never write.

Sleeper's reads are the best of any fantasy platform here — every league, roster,
matchup and draft with no credential at all — and its writes are impossible. Both halves
matter, so both are asserted.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from sportsdata_agents.tools import sleeper as sl

TABLE = {
    "4046": {"full_name": "Patrick Mahomes", "team": "KC", "position": "QB", "status": "Active"},
    "8800": {"full_name": "Malik Davis", "team": None, "position": "RB", "status": "Active"},
    "13602": {"full_name": "Jack Strand", "team": "ATL", "position": "QB", "status": "Active"},
}


@pytest.fixture
def cached(monkeypatch, tmp_path):
    monkeypatch.setattr("sportsdata_agents.paths.data_dir", lambda: tmp_path)
    (tmp_path / "sleeper-players-nfl.json").write_text(json.dumps(TABLE))
    return tmp_path


# ─── it can never write ─────────────────────────────────────────────────


def test_the_sleeper_agent_holds_no_write_tool():
    """Not a phase to be lifted later: Sleeper has no public write API, and the private
    GraphQL the app uses would break without notice on someone's real team."""
    import yaml

    import sportsdata_agents

    spec = yaml.safe_load(
        (Path(sportsdata_agents.__file__).parent / "specs" / "sleeper_manager.yaml").read_text())
    agent = spec["agent"]
    assert not any(g.endswith(".write") for g in agent["tools"]["mcp_groups"])
    assert "sport.transactions" in agent["forbidden_capabilities"]
    assert not any("propose" in t for t in agent["tools"]["native"])


def test_sleeper_has_no_write_group_to_grant():
    """The data plane side of the same claim."""
    import yaml

    spec_path = Path.home() / "Documents/Projects/sportsdata-mcp/src/sportsdata_mcp/specs/sleeper.yaml"
    if not spec_path.exists():
        pytest.skip("data plane not checked out beside this repo")
    spec = yaml.safe_load(spec_path.read_text())
    groups = {e["group"] for e in spec["endpoints"]}
    assert not any(g.endswith(".write") for g in groups)
    assert all(e.get("method", "GET").upper() == "GET" for e in spec["endpoints"])


# ─── resolving ids ──────────────────────────────────────────────────────


async def test_ids_resolve_to_names(cached):
    out = await sl.sleeper_resolve_players({"ids": ["4046", "13602"]})
    assert out["resolved"] == 2
    assert out["players"]["4046"]["name"] == "Patrick Mahomes"
    assert out["players"]["4046"]["team"] == "KC"
    assert "unresolved" not in out


async def test_a_missing_team_is_reported_as_a_free_agent(cached):
    """`team: null` is the difference between a waiver claim and a trade, so it is
    spelled out rather than left as a null for the model to interpret."""
    out = await sl.sleeper_resolve_players({"ids": ["8800"]})
    p = out["players"]["8800"]
    assert p["team"] == "FA"
    assert p["is_free_agent"] is True


async def test_an_unknown_id_is_named_not_silently_dropped(cached):
    """A model that does not know an id went missing will quietly omit a player from a
    lineup and never mention it."""
    out = await sl.sleeper_resolve_players({"ids": ["4046", "999999"]})
    assert out["resolved"] == 1
    assert out["unresolved"] == ["999999"]
    assert "note" in out


async def test_integer_ids_are_accepted(cached):
    """Sleeper ids are strings, but a model that read one out of a roster may hand back
    an int. Failing on that would be a pointless refusal."""
    out = await sl.sleeper_resolve_players({"ids": [4046]})
    assert out["players"]["4046"]["name"] == "Patrick Mahomes"


async def test_an_empty_id_list_is_refused(cached):
    with pytest.raises(ValueError, match="non-empty"):
        await sl.sleeper_resolve_players({"ids": []})


# ─── the reverse lookup ─────────────────────────────────────────────────


async def test_names_resolve_to_ids(cached):
    out = await sl.sleeper_find_players({"query": "mahomes"})
    assert out["matches"][0]["player_id"] == "4046"


async def test_search_is_case_insensitive_and_partial(cached):
    assert (await sl.sleeper_find_players({"query": "MAHOM"}))["total_matches"] == 1


async def test_rostered_players_sort_before_free_agents(cached):
    """A surname search across 12,000 rows is mostly retired and practice-squad names;
    the one you meant is almost always on a team."""
    table = dict(TABLE)
    table["1"] = {"full_name": "Aaron Davis", "team": None, "position": "WR", "status": "Active"}
    table["2"] = {"full_name": "Zeke Davis", "team": "SF", "position": "WR", "status": "Active"}
    (Path(sl.cache_path("nfl"))).write_text(json.dumps(table))
    out = await sl.sleeper_find_players({"query": "davis"})
    assert out["matches"][0]["team"] == "SF", "rostered player must come first"


# ─── the cache ──────────────────────────────────────────────────────────


async def test_a_fresh_cache_is_not_refetched(cached, monkeypatch):
    """One fetch a day is what Sleeper asks for, and the file is 14.6 MB."""
    calls = []

    async def boom(sport="nfl"):
        calls.append(sport)
        return TABLE

    monkeypatch.setattr(sl, "refresh_players", boom)
    await sl.sleeper_resolve_players({"ids": ["4046"]})
    assert calls == [], "a fresh cache must not trigger a refetch"


async def test_a_stale_cache_is_refetched(cached, monkeypatch):
    old = time.time() - (sl.CACHE_TTL_SECONDS + 60)
    import os

    os.utime(sl.cache_path("nfl"), (old, old))
    calls = []

    async def refetch(sport="nfl"):
        calls.append(sport)
        return TABLE

    monkeypatch.setattr(sl, "refresh_players", refetch)
    await sl.sleeper_resolve_players({"ids": ["4046"]})
    assert calls == ["nfl"]


async def test_a_corrupt_cache_costs_one_refetch_not_the_run(cached, monkeypatch):
    sl.cache_path("nfl").write_text("{not json")
    calls = []

    async def refetch(sport="nfl"):
        calls.append(sport)
        return TABLE

    monkeypatch.setattr(sl, "refresh_players", refetch)
    out = await sl.sleeper_resolve_players({"ids": ["4046"]})
    assert calls == ["nfl"]
    assert out["resolved"] == 1


def test_the_fetch_session_raises_its_own_byte_cap():
    """The projected table is ~1.1 MB against a 150 KB default. It never reaches a model
    — it goes to disk — so the cap is raised for that one session rather than globally."""
    assert sl.FETCH_MAX_BYTES > 1_200_000
