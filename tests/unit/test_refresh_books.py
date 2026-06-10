"""`agents refresh-books` — full-catalogue harvest + the lookup_book_ids tool."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from sportsdata_agents.operations.refresh_books import (
    AUTO_BEGIN,
    AUTO_END,
    Probe,
    collect_catalogue,
    find_named_ids,
    rewrite_skill,
    summary_lines,
)
from sportsdata_agents.tools.registry import NATIVE_TOOLS

pytestmark = pytest.mark.unit


# ── tolerant (name, id) discovery — ALL entries, no sport filter ─────────


def test_find_named_ids_collects_everything() -> None:
    """The harvester must not privilege any sport (review finding: it was AFL-only)."""
    payload = {
        "classes": [
            {"id": 50, "name": "Australian Rules"},
            {"id": 9, "name": "Soccer", "competitions": [{"competitionId": 11, "competitionName": "EPL"}]},
            {"key": "basketball", "displayName": "Basketball NBA"},
        ]
    }
    pairs = find_named_ids(payload)
    assert ("Australian Rules", "50") in pairs
    assert ("Soccer", "9") in pairs
    assert ("EPL", "11") in pairs
    assert ("Basketball NBA", "basketball") in pairs


def test_find_named_ids_matches_conventions_not_spellings() -> None:
    """Field detection is convention-based (review finding: an allowlist of spellings
    is the same shell one level down — `label`/`code`/`eventTypeId` must work)."""
    payload = {
        "items": [
            {"label": "Greyhounds", "code": "GR"},
            {"categoryName": "Harness", "eventTypeId": 99},
            {"title": "Esports", "slug": "esports-lol"},
        ]
    }
    pairs = find_named_ids(payload)
    assert ("Greyhounds", "GR") in pairs
    assert ("Harness", "99") in pairs
    assert ("Esports", "esports-lol") in pairs


def test_find_named_ids_prefers_own_id_and_sane_names() -> None:
    # a node with its own `id` AND a foreign `competitionId` pairs with its OWN id
    payload = {"x": [{"name": "Round 14", "id": 7, "competitionId": 4165}]}
    assert find_named_ids(payload) == [("Round 14", "7")]
    # numeric "names" are not display names; id values must be scalar and short
    junk = {"y": [{"name": "12345", "id": 1}, {"name": "Real", "id": {"nested": True}}]}
    assert find_named_ids(junk) == []


def test_value_inference_handles_alien_spellings() -> None:
    """Keys OUTSIDE any convention we could list — the values themselves identify
    the columns (ids: unique short scalars; names: letterful strings)."""
    payload = {
        "stuff": [
            {"caption": "Soccer", "ref": 9, "active": True},
            {"caption": "Basketball", "ref": 11, "active": True},
            {"caption": "Aussie Rules", "ref": 50, "active": False},
        ]
    }
    pairs = find_named_ids(payload)
    assert ("Soccer", "9") in pairs and ("Aussie Rules", "50") in pairs


def test_value_inference_with_uuid_and_slug_ids() -> None:
    payload = {
        "x": [
            {"desc": "Racing NSW", "uuidv": "23d497e6-8aab-4309-905b-9421f42c9bc5"},
            {"desc": "Racing VIC", "uuidv": "02e4abac-1f27-4afb-ac12-7eb470f2d941"},
            {"desc": "Racing QLD", "uuidv": "8c5dc0e5-0b30-4a76-a97b-287c52b7d6a1"},
        ]
    }
    assert ("Racing VIC", "02e4abac-1f27-4afb-ac12-7eb470f2d941") in find_named_ids(payload)


def test_value_inference_refuses_ambiguous_arrays() -> None:
    """No letterful column / no unique-scalar column → no fabricated pairs."""
    measurements = {"m": [{"value": 1.2, "ts": 100}, {"value": 1.3, "ts": 101}, {"value": 1.2, "ts": 102}]}
    assert find_named_ids(measurements) == []
    # two name-ish columns but ids non-unique → refuse rather than guess
    dupes = {"d": [{"caption": "A", "ref": 1}, {"caption": "B", "ref": 1}, {"caption": "C", "ref": 1}]}
    assert find_named_ids(dupes) == []


def test_conventions_still_win_when_present() -> None:
    """Inference only fires where conventions are blind (no double-extraction)."""
    payload = {"a": [{"name": "AFL", "id": 1, "extra": "x1"}, {"name": "NBA", "id": 2, "extra": "x2"},
                     {"name": "NRL", "id": 3, "extra": "x3"}]}
    assert find_named_ids(payload) == [("AFL", "1"), ("NBA", "2"), ("NRL", "3")]


def test_find_named_ids_dedupes() -> None:
    payload = {"a": [{"name": "AFL", "id": 1}, {"name": "AFL", "id": 1}]}
    assert find_named_ids(payload) == [("AFL", "1")]


# ── catalogue collection (fake manager) ──────────────────────────────────


class FakeManager:
    def __init__(self, payloads: dict[str, Any]) -> None:
        self.payloads = payloads

    async def call_tool(self, name: str, args: Any = None) -> Any:
        result = self.payloads[name]
        if isinstance(result, Exception):
            raise result
        return result


PROBES = [
    Probe("Sportsbet", "sb_nav", {}),
    Probe("TAB", "tab_sports", {}),
]


async def test_collect_catalogue_records_entries_and_failures() -> None:
    manager = FakeManager(
        {
            "sb_nav": {"classes": [{"id": 4165, "name": "AFL"}, {"id": 9, "name": "Soccer"}]},
            "tab_sports": RuntimeError("akamai says no"),
        }
    )
    cat = await collect_catalogue(manager, PROBES, today=dt.date(2026, 6, 10))  # type: ignore[arg-type]
    assert cat["Sportsbet"]["entries"] == [["AFL", "4165"], ["Soccer", "9"]]
    assert cat["Sportsbet"]["fetched_at"] == "2026-06-10"
    assert cat["TAB"]["error"].startswith("RuntimeError")
    lines = summary_lines(cat)
    assert "2 named ids harvested" in lines[0]
    assert "probe failed" in lines[1]


# ── marker rewriting ─────────────────────────────────────────────────────


SKILL = f"""---
name: x
description: d
triggers: [afl]
---
# Nav

hand-written intro

{AUTO_BEGIN}
old auto content
{AUTO_END}

hand-written outro
"""


def test_rewrite_replaces_only_the_auto_section(tmp_path: Path) -> None:
    p = tmp_path / "SKILL.md"
    p.write_text(SKILL, encoding="utf-8")
    cat = {"Sportsbet": {"tool": "sb_nav", "fetched_at": "2026-06-10", "entries": [["AFL", "4165"]]}}
    rewrite_skill(p, cat, today=dt.date(2026, 6, 10))
    out = p.read_text()
    assert "hand-written intro" in out and "hand-written outro" in out
    assert "old auto content" not in out
    assert "auto-verified 2026-06-10" in out.lower()
    assert "lookup_book_ids" in out  # the skill points agents at the tool
    # idempotent
    rewrite_skill(p, cat, today=dt.date(2026, 6, 17))
    assert p.read_text().count(AUTO_BEGIN) == 1


def test_rewrite_without_markers_fails_loudly(tmp_path: Path) -> None:
    p = tmp_path / "SKILL.md"
    p.write_text("no markers here", encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        rewrite_skill(p, {})


def test_bundled_skill_carries_the_markers() -> None:
    from sportsdata_agents.agents.skills import builtin_skills_dir

    text = (builtin_skills_dir() / "book_navigation" / "SKILL.md").read_text(encoding="utf-8")
    assert AUTO_BEGIN in text and AUTO_END in text


# ── the lookup_book_ids native tool ──────────────────────────────────────


@pytest.fixture
def catalogue_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cat = {
        "Sportsbet": {"tool": "sb_nav", "fetched_at": "2026-06-10",
                      "entries": [["AFL", "4165"], ["NBA", "6927"], ["AFL Brownlow Medal", "6136"]]},
        "TAB": {"tool": "tab_sports", "fetched_at": "2026-06-10",
                "entries": [["AFL Football", "1"], ["Basketball", "11"]]},
    }
    path = tmp_path / "CATALOGUE.json"
    path.write_text(json.dumps(cat), encoding="utf-8")
    import sportsdata_agents.operations.refresh_books as rb

    monkeypatch.setattr(rb, "catalogue_path", lambda: path)
    return path


async def test_lookup_matches_across_books(catalogue_file: Path) -> None:
    out = await NATIVE_TOOLS["lookup_book_ids"].execute({"query": "afl"})
    assert {"name": "AFL", "id": "4165"} in out["matches"]["Sportsbet"]["matches"]
    assert {"name": "AFL Football", "id": "1"} in out["matches"]["TAB"]["matches"]
    assert out["matches"]["Sportsbet"]["fetched_at"] == "2026-06-10"  # provenance rides along


async def test_lookup_book_filter_and_other_sports(catalogue_file: Path) -> None:
    out = await NATIVE_TOOLS["lookup_book_ids"].execute({"query": "nba", "book": "sportsbet"})
    assert list(out["matches"]) == ["Sportsbet"]
    out2 = await NATIVE_TOOLS["lookup_book_ids"].execute({"query": "basketball"})
    assert "TAB" in out2["matches"]  # any sport works — not just AFL


async def test_lookup_no_match_is_helpful(catalogue_file: Path) -> None:
    out = await NATIVE_TOOLS["lookup_book_ids"].execute({"query": "quidditch"})
    assert out["matches"] == {} and "broader" in out["note"]


async def test_lookup_missing_catalogue_is_actionable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sportsdata_agents.operations.refresh_books as rb

    monkeypatch.setattr(rb, "catalogue_path", lambda: tmp_path / "nope.json")
    with pytest.raises(FileNotFoundError, match="refresh-books"):
        await NATIVE_TOOLS["lookup_book_ids"].execute({"query": "afl"})
