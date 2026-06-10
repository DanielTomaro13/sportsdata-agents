"""Weekly book-catalogue refresh (`agents refresh-books`).

Probes each bookmaker's *discovery* routes through the MCP and harvests EVERY
(name, id) pair they expose — all sports, all competitions — into
``skills/book_navigation/CATALOGUE.json``. Agents resolve ids at runtime with the
``lookup_book_ids`` native tool (only query matches enter model context, never the
full catalogue — §8.2). The skill's auto-section carries a per-book summary with a
freshness stamp. Deterministic: no LLM involved.

Run weekly (cron / launchd):  ``agents refresh-books``
At P3 the ops agents take over running it and opening a PR when ids drift; P2's
ingestion store supersedes most of it for odds.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sportsdata_agents.agents.skills import builtin_skills_dir
from sportsdata_agents.mcp.manager import MCPManager

logger = logging.getLogger(__name__)

AUTO_BEGIN = "<!-- AUTO:BEGIN refresh-books -->"
AUTO_END = "<!-- AUTO:END refresh-books -->"

# Key-name heuristics for walking unknown discovery shapes. These are about JSON
# STRUCTURE (which fields mean "name"/"id"), not about which sports matter.
_NAME_KEYS = ("name", "displayName", "competitionName", "className", "title")
_ID_KEYS = ("id", "key", "competitionId", "competitionKey", "classId", "slug")


def find_named_ids(payload: Any, pattern: re.Pattern[str] | None = None) -> list[tuple[str, str]]:
    """Recursively collect (name, id) pairs — ALL of them unless ``pattern`` filters.

    Discovery payload shapes differ per book; a tolerant walk beats per-book
    parsers that break on cosmetic upstream changes.
    """
    found: list[tuple[str, str]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            name = next((str(node[k]) for k in _NAME_KEYS if isinstance(node.get(k), str)), None)
            if name and (pattern is None or pattern.search(name)):
                id_ = next((str(node[k]) for k in _ID_KEYS if node.get(k) is not None), None)
                if id_ is not None:
                    found.append((name, id_))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    # de-dup preserving order
    seen: set[tuple[str, str]] = set()
    return [p for p in found if not (p in seen or seen.add(p))]  # type: ignore[func-returns-value]


@dataclass(frozen=True)
class Probe:
    book: str
    tool: str
    arguments: dict[str, Any]


def build_probes(today: dt.date | None = None) -> list[Probe]:
    """Discovery routes (cheap list endpoints — never the price firehoses)."""
    d = today or dt.date.today()
    return [
        # NB: sportsbet_sports_classes 400s upstream as of 2026-06-10 (endpoint
        # drift — every date format rejected); NavHierarchy carries the same ids.
        Probe("Sportsbet", "sportsbet_nav_hierarchy", {}),
        # the feed ignores the date token's value but requires its presence (ddMMMyyyy)
        Probe("PointsBet", "pointsbet_sports_list", {"date": d.strftime("%d%b%Y")}),
        Probe("TAB", "tab_sports", {}),
    ]


def catalogue_path() -> Path:
    return builtin_skills_dir() / "book_navigation" / "CATALOGUE.json"


async def collect_catalogue(
    manager: MCPManager, probes: Iterable[Probe] | None = None, *, today: dt.date | None = None
) -> dict[str, Any]:
    """{book: {tool, fetched_at, error?, entries: [[name, id], ...]}} for every probe."""
    probes = build_probes(today) if probes is None else probes
    stamp = (today or dt.date.today()).isoformat()
    catalogue: dict[str, Any] = {}
    for probe in probes:
        record: dict[str, Any] = {"tool": probe.tool, "fetched_at": stamp, "entries": []}
        try:
            payload = await manager.call_tool(probe.tool, probe.arguments)
            record["entries"] = [list(p) for p in find_named_ids(payload)]
        except Exception as e:
            logger.warning("probe %s/%s failed: %s", probe.book, probe.tool, e)
            record["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        catalogue[probe.book] = record
    return catalogue


def summary_lines(catalogue: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for book, record in catalogue.items():
        if record.get("error"):
            lines.append(f"- **{book}** (`{record['tool']}`): probe failed — {record['error'].split(':')[0]}")
        elif not record["entries"]:
            lines.append(f"- **{book}** (`{record['tool']}`): no entries found — discovery may have drifted!")
        else:
            lines.append(f"- **{book}** (`{record['tool']}`): {len(record['entries'])} named ids harvested")
    return lines


def render_auto_section(catalogue: dict[str, Any], *, today: dt.date | None = None) -> str:
    stamp = (today or dt.date.today()).isoformat()
    body = "\n".join(summary_lines(catalogue))
    return (
        f"{AUTO_BEGIN}\n*Catalogue auto-verified {stamp} by `agents refresh-books`:*\n\n{body}\n\n"
        f"Resolve ANY sport/competition/market id with the `lookup_book_ids` tool "
        f"(e.g. query \"NBA\", \"AFL\", \"rugby\") instead of guessing.\n{AUTO_END}"
    )


def rewrite_skill(skill_path: Path, catalogue: dict[str, Any], *, today: dt.date | None = None) -> None:
    text = skill_path.read_text(encoding="utf-8")
    if AUTO_BEGIN not in text or AUTO_END not in text:
        raise ValueError(f"{skill_path}: missing {AUTO_BEGIN}/{AUTO_END} markers")
    pattern = re.compile(re.escape(AUTO_BEGIN) + r".*?" + re.escape(AUTO_END), re.DOTALL)
    skill_path.write_text(pattern.sub(render_auto_section(catalogue, today=today), text), encoding="utf-8")


async def refresh_books(mcp_command: list[str] | None = None) -> dict[str, Any]:
    """Probe, persist the catalogue, refresh the skill summary; returns the catalogue."""
    skill_dir = builtin_skills_dir() / "book_navigation"
    # The response cap protects MODEL context; this is a deterministic consumer —
    # discovery lists (150-300 KB) must come through whole.
    async with MCPManager(
        groups=[], command=mcp_command, extra_env={"SPORTSDATA_MCP_MAX_BYTES": "0"}
    ) as manager:
        catalogue = await collect_catalogue(manager)
    catalogue_path().write_text(json.dumps(catalogue, indent=1), encoding="utf-8")
    rewrite_skill(skill_dir / "SKILL.md", catalogue)
    return catalogue
