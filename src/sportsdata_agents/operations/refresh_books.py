"""Weekly book-catalogue refresh (`agents refresh-books`).

Probes each bookmaker's *discovery* routes through the MCP and regenerates the
auto-managed section of ``skills/book_navigation/SKILL.md`` — so agents navigate
with verified competition ids instead of guessing (and id drift is caught weekly
instead of mid-run). Deterministic: no LLM involved. Prose outside the markers is
hand-maintained and survives every refresh.

Run weekly (cron / launchd):  ``agents refresh-books``
At P3 the ops agents take over running it and opening a PR when ids drift; P2's
ingestion store supersedes most of it for odds.
"""

from __future__ import annotations

import datetime as dt
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

# Patterns that identify AFL-ish entries in discovery payloads.
_AFL_RE = re.compile(r"\b(AFL|Australian Rules|Aussie Rules)\b", re.IGNORECASE)
_NAME_KEYS = ("name", "displayName", "competitionName", "className")
_ID_KEYS = ("id", "key", "competitionId", "competitionKey", "classId")


def find_named_ids(payload: Any, pattern: re.Pattern[str] = _AFL_RE) -> list[tuple[str, str]]:
    """Recursively find (name, id) pairs whose name matches ``pattern``.

    Discovery payload shapes differ per book; a tolerant walk beats per-book
    parsers that break on cosmetic upstream changes.
    """
    found: list[tuple[str, str]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            name = next((str(node[k]) for k in _NAME_KEYS if isinstance(node.get(k), str)), None)
            if name and pattern.search(name):
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


async def collect_lines(manager: MCPManager, probes: Iterable[Probe] | None = None) -> list[str]:
    """One markdown bullet per discovery, or an explicit probe-failure line."""
    probes = build_probes() if probes is None else probes
    lines: list[str] = []
    for probe in probes:
        try:
            payload = await manager.call_tool(probe.tool, probe.arguments)
            pairs = find_named_ids(payload)
            if pairs:
                ids = "; ".join(f"{name} = `{id_}`" for name, id_ in pairs[:6])
                lines.append(f"- **{probe.book}** (`{probe.tool}`): {ids}")
            else:
                lines.append(f"- **{probe.book}** (`{probe.tool}`): no AFL entries found — id may have drifted!")
        except Exception as e:
            logger.warning("probe %s/%s failed: %s", probe.book, probe.tool, e)
            lines.append(f"- **{probe.book}** (`{probe.tool}`): probe failed — {type(e).__name__}")
    return lines


def render_auto_section(lines: list[str], *, today: dt.date | None = None) -> str:
    stamp = (today or dt.date.today()).isoformat()
    body = "\n".join(lines)
    return f"{AUTO_BEGIN}\n*Auto-verified {stamp} by `agents refresh-books`:*\n\n{body}\n{AUTO_END}"


def rewrite_skill(skill_path: Path, lines: list[str], *, today: dt.date | None = None) -> None:
    text = skill_path.read_text(encoding="utf-8")
    if AUTO_BEGIN not in text or AUTO_END not in text:
        raise ValueError(f"{skill_path}: missing {AUTO_BEGIN}/{AUTO_END} markers")
    pattern = re.compile(re.escape(AUTO_BEGIN) + r".*?" + re.escape(AUTO_END), re.DOTALL)
    skill_path.write_text(pattern.sub(render_auto_section(lines, today=today), text), encoding="utf-8")


async def refresh_books(mcp_command: list[str] | None = None) -> list[str]:
    """Probe and rewrite; returns the generated lines (CLI prints them)."""
    skill_path = builtin_skills_dir() / "book_navigation" / "SKILL.md"
    # The response cap protects MODEL context; this is a deterministic consumer —
    # discovery lists (150-300 KB) must come through whole.
    async with MCPManager(
        groups=[], command=mcp_command, extra_env={"SPORTSDATA_MCP_MAX_BYTES": "0"}
    ) as manager:
        lines = await collect_lines(manager)
    rewrite_skill(skill_path, lines)
    return lines
