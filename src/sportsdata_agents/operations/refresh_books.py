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

# Convention-based field detection — NOT an allowlist of spellings (a book using
# `label`/`code`/`eventTypeId`/`categoryName` must still be harvested). We match how
# APIs *name* fields, then sanity-check the values.
_NAME_KEY_RE = re.compile(r"name|title|label|display", re.IGNORECASE)
_ID_KEY_RE = re.compile(r"(?:id|key|slug|code)$", re.IGNORECASE)


def _name_field(node: dict) -> str | None:
    """The most generic name-ish string field ('name' beats 'venueName')."""
    candidates = [
        k for k, v in node.items()
        if _NAME_KEY_RE.search(k) and isinstance(v, str) and v.strip() and re.search(r"[A-Za-z]", v)
    ]
    return min(candidates, key=len) if candidates else None


def _id_field(node: dict) -> str | None:
    """The most generic id-ish scalar field — 'id' beats 'competitionId', so a node
    pairs with its OWN id, not a foreign reference."""
    candidates = [
        k for k, v in node.items()
        if _ID_KEY_RE.search(k) and isinstance(v, (str, int)) and 0 < len(str(v)) < 64
    ]
    return min(candidates, key=len) if candidates else None


# ── Layer 2: value-based inference (NO key knowledge) ─────────────────────
# When conventions find nothing in an array of similar objects, the columns reveal
# themselves by their VALUES: ids are unique short scalars (ints / uuids / slugs);
# names are letterful human-word strings. Schema inference from data, not vocabulary
# — survives spellings nobody anticipated.

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)+$")


def _id_value_score(values: list[Any]) -> float:
    scalars = [v for v in values if isinstance(v, (str, int)) and 0 < len(str(v)) < 64]
    if len(scalars) < len(values) * 0.8:
        return 0.0
    uniqueness = len({str(v) for v in scalars}) / len(scalars)
    fmt = sum(
        1 for v in scalars
        if isinstance(v, int) or str(v).isdigit() or _UUID_RE.match(str(v)) or _SLUG_RE.match(str(v))
    ) / len(scalars)
    return uniqueness * (0.5 + 0.5 * fmt)


def _name_value_score(values: list[Any]) -> float:
    strings = [v for v in values if isinstance(v, str) and v.strip()]
    if len(strings) < len(values) * 0.8:
        return 0.0
    letterful = sum(
        1 for v in strings
        if sum(c.isalpha() or c.isspace() for c in v) / len(v) > 0.5 and not v.isdigit()
    ) / len(strings)
    uniqueness = len(set(strings)) / len(strings)
    return letterful * (0.5 + 0.5 * uniqueness)


def infer_pairs(rows: list[dict]) -> list[tuple[str, str]]:
    """(name, id) pairs from an array of similar objects, by value analysis alone."""
    if len(rows) < 3:
        return []
    common = [k for k in rows[0] if sum(k in r for r in rows) >= len(rows) * 0.8]
    if len(common) < 2:
        return []
    id_scores = {k: _id_value_score([r[k] for r in rows if k in r]) for k in common}
    name_scores = {k: _name_value_score([r[k] for r in rows if k in r]) for k in common}
    id_key = max(id_scores, key=lambda k: id_scores[k])
    name_key = max(name_scores, key=lambda k: name_scores[k])
    if id_key == name_key or id_scores[id_key] < 0.8 or name_scores[name_key] < 0.8:
        return []
    return [
        (str(r[name_key]), str(r[id_key]))
        for r in rows
        if name_key in r and id_key in r and isinstance(r[name_key], str) and r[name_key].strip()
    ]


def find_named_ids(payload: Any, pattern: re.Pattern[str] | None = None) -> list[tuple[str, str]]:
    """Recursively collect (name, id) pairs — ALL of them unless ``pattern`` filters.

    Two layers: key-naming conventions first (free, ~95% of APIs); for arrays where
    conventions found NOTHING, value-based inference takes over — so spellings
    outside any list we could write still harvest. (Layer 3 — LLM schema-mapping by
    the ops agents when both fail — is planned at P3.)
    """
    found: list[tuple[str, str]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            name_key = _name_field(node)
            if name_key is not None:
                name = str(node[name_key])
                if pattern is None or pattern.search(name):
                    id_key = _id_field(node)
                    if id_key is not None:
                        found.append((name, str(node[id_key])))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            rows = [item for item in node if isinstance(item, dict)]
            if rows and not any(_name_field(r) and _id_field(r) for r in rows):
                # conventions are blind here — let the values speak
                for name, id_ in infer_pairs(rows):
                    if pattern is None or pattern.search(name):
                        found.append((name, id_))
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
