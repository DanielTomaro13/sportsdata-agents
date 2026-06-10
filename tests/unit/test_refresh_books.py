"""`agents refresh-books` — tolerant discovery parsing + marker rewriting."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from sportsdata_agents.operations.refresh_books import (
    AUTO_BEGIN,
    AUTO_END,
    Probe,
    collect_lines,
    find_named_ids,
    rewrite_skill,
)

pytestmark = pytest.mark.unit


# ── tolerant (name, id) discovery ────────────────────────────────────────


def test_find_named_ids_across_shapes() -> None:
    """One walker handles every book's discovery shape."""
    sportsbet = {"classes": [{"id": 23, "name": "Australian Rules"}, {"id": 9, "name": "Soccer"}]}
    pointsbet = {"locales": [{"competitions": [{"key": 7523, "name": "AFL"}]}]}
    tab = {"sports": [{"displayName": "AFL Football"}, {"displayName": "Basketball"}]}

    assert find_named_ids(sportsbet) == [("Australian Rules", "23")]
    assert find_named_ids(pointsbet) == [("AFL", "7523")]
    assert find_named_ids(tab) == []  # name matched but no id key — not a pair


def test_find_named_ids_dedupes_and_prefers_first_id_key() -> None:
    payload = {"a": [{"name": "AFL", "id": 1, "key": 2}, {"name": "AFL", "id": 1, "key": 2}]}
    assert find_named_ids(payload) == [("AFL", "1")]


# ── probing (fake manager) ───────────────────────────────────────────────


class FakeManager:
    def __init__(self, payloads: dict[str, Any]) -> None:
        self.payloads = payloads

    async def call_tool(self, name: str, args: Any = None) -> Any:
        result = self.payloads[name]
        if isinstance(result, Exception):
            raise result
        return result


async def test_collect_lines_reports_hits_misses_and_failures() -> None:
    probes = [
        Probe("Sportsbet", "sb_classes", {}),
        Probe("PointsBet", "pb_sports", {}),
        Probe("TAB", "tab_sports", {}),
    ]
    manager = FakeManager(
        {
            "sb_classes": {"classes": [{"id": 4165, "name": "AFL"}]},
            "pb_sports": {"sports": [{"name": "Soccer", "key": 1}]},  # no AFL → drift warning
            "tab_sports": RuntimeError("akamai says no"),
        }
    )
    lines = await collect_lines(manager, probes)  # type: ignore[arg-type]
    assert lines[0] == "- **Sportsbet** (`sb_classes`): AFL = `4165`"
    assert "id may have drifted" in lines[1]
    assert lines[2] == "- **TAB** (`tab_sports`): probe failed — RuntimeError"


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
    rewrite_skill(p, ["- **Sportsbet**: AFL = `4165`"], today=dt.date(2026, 6, 10))
    out = p.read_text()
    assert "hand-written intro" in out and "hand-written outro" in out
    assert "old auto content" not in out
    assert "Auto-verified 2026-06-10" in out
    assert "- **Sportsbet**: AFL = `4165`" in out
    # idempotent: a second rewrite still finds exactly one marker pair
    rewrite_skill(p, ["- updated"], today=dt.date(2026, 6, 17))
    out2 = p.read_text()
    assert out2.count(AUTO_BEGIN) == 1 and "- updated" in out2 and "4165" not in out2


def test_rewrite_without_markers_fails_loudly(tmp_path: Path) -> None:
    p = tmp_path / "SKILL.md"
    p.write_text("no markers here", encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        rewrite_skill(p, ["x"])


def test_bundled_skill_carries_the_markers() -> None:
    from sportsdata_agents.agents.skills import builtin_skills_dir

    text = (builtin_skills_dir() / "book_navigation" / "SKILL.md").read_text(encoding="utf-8")
    assert AUTO_BEGIN in text and AUTO_END in text
