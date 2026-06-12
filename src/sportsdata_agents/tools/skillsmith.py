"""Skill learning loop (the generalist's growth mechanism).

A *skill* is a prose playbook (SKILL.md) — never code. When the generalist cracks
a reusable method it writes one with ``create_skill``; later it pulls the relevant
ones back into context with ``list_skills`` + ``recall_skill``. The platform "grows
to the user's needs" by accumulating these self-authored playbooks in the user's
own data dir (``skills_dir()``) — local, private, durable across sessions.

Why this and not the spec ``skills:`` list: that list is validated against the
PACKAGED skills at lint time and resolved from the package at runtime — it can't
reference a skill authored after the build. Recall-on-demand sidesteps that and is
the right progressive-disclosure shape: the index is cheap, bodies load when asked.

Safety: skills are markdown, not executable. A recalled skill is *guidance* in
context — it cannot grant a tool the agent's spec doesn't have, and it can never
bypass the deny-filter on money-verb tools or the advisory invariant. Built-in
skills are never shadowed; names are slug-validated (no path traversal).
"""

from __future__ import annotations

import contextlib
import re
from typing import Any

import yaml

from sportsdata_agents.agents.harness import ToolDef

SKILLSMITH_TOOL_NAMES = {"create_skill", "list_skills", "recall_skill"}

_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{1,48}")

# growth-loop hints: when a learned skill keeps getting recalled it's a recurring
# need that may deserve its own agent; when the library sprawls, suggest a prune.
PROMOTE_NUDGE_AT = 3
LIBRARY_HINT_AT = 100


def _validate_name(name: str) -> str:
    name = str(name).strip().lower()
    if not _NAME.fullmatch(name):
        raise ValueError(
            f"skill name {name!r} must be a slug: lowercase letters/digits/_/- , 2-49 chars"
        )
    return name


def _one_line(value: str, cap: int) -> str:
    """Collapse whitespace to a single line and cap length. Frontmatter fields are
    one-liners; a newline in one would otherwise break out of the YAML block."""
    return " ".join(str(value).split())[:cap]


def _user_skill_path(name: str) -> Any:
    from sportsdata_agents.paths import skills_dir

    return skills_dir() / name / "SKILL.md"


def _recall_counts() -> dict[str, int]:
    """How often each LEARNED skill has been recalled (the promotion signal).
    A corrupt counts file resets — the count is a hint, never load-bearing."""
    import json

    from sportsdata_agents.paths import skills_dir

    path = skills_dir() / ".recalls.json"
    if path.is_file():
        with contextlib.suppress(Exception):
            return {str(k): int(v) for k, v in json.loads(path.read_text(encoding="utf-8")).items()}
    return {}


def _save_recall_counts(counts: dict[str, int]) -> None:
    import json

    from sportsdata_agents.paths import skills_dir

    with contextlib.suppress(OSError):  # a hint, never worth failing a recall over
        (skills_dir() / ".recalls.json").write_text(json.dumps(counts), encoding="utf-8")


async def create_skill(args: dict[str, Any]) -> Any:
    """{name, description, triggers: [str], body} → persist a reusable PROSE
    playbook to the user's skill library (no code). Validated and slug-named;
    a built-in skill is never overwritten. Recall it later with recall_skill."""
    from sportsdata_agents.agents.skills import builtin_skills_dir, parse_skill_md

    name = _validate_name(args.get("name", ""))
    description = _one_line(args.get("description", ""), 200)
    body = str(args.get("body", "")).strip()
    # one line each, deduped, capped count — a trigger is a keyword, not a sentence
    triggers = list(dict.fromkeys(t for t in (_one_line(t, 60) for t in (args.get("triggers") or [])) if t))[:12]
    if not description:
        raise ValueError("description is required (one line — what the skill is for)")
    if not body:
        raise ValueError("body is required (the playbook itself)")
    if not triggers:
        raise ValueError("triggers is required (keywords that should surface this skill)")
    if (builtin_skills_dir() / name / "SKILL.md").is_file():
        raise ValueError(f"{name!r} is a built-in skill — pick another name (built-ins are not overwritten)")
    if len(body) > 20_000:
        raise ValueError("body too long — a skill is a tight playbook, not a document")

    # Build the frontmatter with safe_dump, NOT string concatenation: a newline or a
    # colon in a value would otherwise break out of the YAML block (inject a key).
    front = yaml.safe_dump(
        {"name": name, "description": description, "triggers": triggers},
        sort_keys=False, allow_unicode=True, default_flow_style=False,
    ).strip()
    text = f"---\n{front}\n---\n\n{body}\n"
    parse_skill_md(text, source=f"<create_skill {name}>")  # validate before writing

    path = _user_skill_path(name)
    updated = path.is_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    note = ("refined an existing skill" if updated
            else "available to recall_skill from now on (this and future sessions)")
    library = sum(1 for e in path.parent.parent.iterdir() if (e / "SKILL.md").is_file())
    if library >= LIBRARY_HINT_AT:
        note += (f" · the library now holds {library} learned skills — suggest the user prunes "
                 "stale ones (agents skills --remove <name>)")
    return {"saved": str(path), "name": name, "updated": updated, "note": note}


def remove_skill(name: str) -> dict[str, Any]:
    """Delete a USER skill by name. Built-in skills are protected. Returns whether
    a file was removed. Destructive, so this is user-initiated (the `agents skills
    --remove` CLI), not an agent tool."""
    from sportsdata_agents.agents.skills import builtin_skills_dir
    from sportsdata_agents.paths import skills_dir

    name = _validate_name(name)
    if (builtin_skills_dir() / name / "SKILL.md").is_file():
        raise ValueError(f"{name!r} is a built-in skill — it cannot be removed")
    skill_dir = skills_dir() / name
    doc = skill_dir / "SKILL.md"
    if not doc.is_file():
        return {"removed": False, "name": name, "note": "no such learned skill"}
    doc.unlink()
    # tidy up the now-empty skill directory (ignore anything else the user put there)
    with contextlib.suppress(OSError):
        skill_dir.rmdir()
    counts = _recall_counts()
    if counts.pop(name, None) is not None:
        _save_recall_counts(counts)
    return {"removed": True, "name": name}


async def list_skills(args: dict[str, Any]) -> Any:
    """List every skill the platform knows — built-in playbooks and the ones the
    generalist has authored — as {name, description, source}. Check this at the
    start of a novel task to reuse what's already been learned."""
    from sportsdata_agents.agents.skills import builtin_skills_dir, parse_skill_md
    from sportsdata_agents.paths import skills_dir

    counts = _recall_counts()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source, root in (("user", skills_dir()), ("builtin", builtin_skills_dir())):
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            doc = entry / "SKILL.md"
            if entry.name in seen or not doc.is_file():
                continue
            try:
                skill = parse_skill_md(doc.read_text(encoding="utf-8"), source=str(doc))
            except Exception:  # a malformed skill must not break discovery
                continue
            seen.add(entry.name)
            row: dict[str, Any] = {"name": skill.name, "description": skill.description, "source": source}
            if source == "user":
                row["recalls"] = counts.get(skill.name, 0)
            out.append(row)
    return {"skills": out}


async def recall_skill(args: dict[str, Any]) -> Any:
    """{name} → the full playbook for one skill (user library first, then built-in).
    Pull a skill in when its method applies to the task at hand."""
    from sportsdata_agents.agents.skills import builtin_skills_dir, load_skill
    from sportsdata_agents.paths import skills_dir

    name = _validate_name(args.get("name", ""))
    for root in (skills_dir(), builtin_skills_dir()):
        if (root / name / "SKILL.md").is_file():
            skill = load_skill(name, root)
            out: dict[str, Any] = {"name": skill.name, "description": skill.description, "body": skill.body}
            if root == skills_dir():  # learned skill: count the recall as a promotion signal
                counts = _recall_counts()
                counts[name] = counts.get(name, 0) + 1
                _save_recall_counts(counts)
                if counts[name] >= PROMOTE_NUDGE_AT:
                    out["note"] = (
                        f"this skill has been recalled {counts[name]} times — a recurring need. "
                        "Consider promoting it into a dedicated agent (draft_agent_spec → "
                        "save_agent_spec) so it gets its own data scope and routine."
                    )
            return out
    raise FileNotFoundError(f"no skill named {name!r} — use list_skills to see what exists")


SKILLSMITH_TOOLS: dict[str, ToolDef] = {
    "create_skill": ToolDef(
        name="create_skill",
        description=(
            "Persist a reusable method as a prose skill (SKILL.md) in the user's library, so the "
            "platform reuses it next time. Use ONLY for genuinely reusable methods, not one-offs."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "slug, e.g. 'expected_goals_method'"},
                "description": {"type": "string", "description": "one line — what it's for"},
                "triggers": {"type": "array", "items": {"type": "string"},
                             "description": "keywords that should surface this skill later"},
                "body": {"type": "string", "description": "the playbook (markdown, no code execution)"},
            },
            "required": ["name", "description", "triggers", "body"],
        },
        execute=create_skill,
    ),
    "list_skills": ToolDef(
        name="list_skills",
        description="List all known skills (built-in + ones you've authored) to reuse learned methods.",
        parameters={"type": "object", "properties": {}, "required": []},
        execute=list_skills,
    ),
    "recall_skill": ToolDef(
        name="recall_skill",
        description="Load one skill's full playbook into context by name.",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        execute=recall_skill,
    ),
}
