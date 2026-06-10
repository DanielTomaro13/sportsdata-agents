"""Agent Skills — progressively-disclosed capability bundles (§8.2, D29).

A skill is a directory ``skills/<name>/SKILL.md``: YAML frontmatter (description +
trigger keywords) and a markdown body of instructions. Only the one-line **index**
rides in the system prompt; the **body** is loaded just-in-time when a trigger matches
the conversation — that's the progressive disclosure that keeps context lean. Skill
*scripts* run in the sandbox (M1.3); this module is discovery + matching + loading.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml

FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)


class SkillError(ValueError):
    """A skill failed to load; the message includes the source path."""

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    triggers: tuple[str, ...]
    body: str

    def matches(self, text: str) -> bool:
        # Word-boundary matching: "vig" must not fire on "na·vig·ation".
        lowered = text.lower()
        return any(re.search(rf"\b{re.escape(t)}\b", lowered) for t in self.triggers)


def parse_skill_md(text: str, *, source: str = "<string>") -> Skill:
    m = FRONTMATTER.match(text)
    if not m:
        raise SkillError(source, "SKILL.md must start with `---` YAML frontmatter followed by a body")
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise SkillError(source, f"invalid frontmatter YAML: {e}") from e
    name = meta.get("name")
    description = meta.get("description")
    triggers = meta.get("triggers")
    if not (isinstance(name, str) and name):
        raise SkillError(source, "frontmatter must set `name`")
    if not (isinstance(description, str) and description):
        raise SkillError(source, "frontmatter must set `description`")
    if not (isinstance(triggers, list) and triggers and all(isinstance(t, str) for t in triggers)):
        raise SkillError(source, "frontmatter must set `triggers` (non-empty list of strings)")
    return Skill(
        name=name,
        description=description,
        triggers=tuple(t.lower() for t in triggers),
        body=m.group(2).strip(),
    )


def builtin_skills_dir() -> Path:
    return Path(str(resources.files("sportsdata_agents"))) / "skills"


def load_skill(name: str, root: Path) -> Skill:
    path = root / name / "SKILL.md"
    if not path.is_file():
        raise SkillError(str(path), f"skill {name!r} not found")
    skill = parse_skill_md(path.read_text(encoding="utf-8"), source=str(path))
    if skill.name != name:
        raise SkillError(str(path), f"frontmatter name {skill.name!r} != directory name {name!r}")
    return skill


class SkillSet:
    """The skills granted to one agent: an index for the prompt + JIT body loading."""

    def __init__(self, skills: list[Skill]) -> None:
        self._skills = {s.name: s for s in skills}
        self._loaded: set[str] = set()

    def __len__(self) -> int:
        return len(self._skills)

    def index_text(self) -> str:
        """One line per skill — all the context the system prompt pays for up front."""
        if not self._skills:
            return ""
        lines = [f"- {s.name}: {s.description}" for s in self._skills.values()]
        return "Skills available (their full instructions load automatically when relevant):\n" + "\n".join(lines)

    def newly_triggered(self, text: str) -> list[Skill]:
        """Skills whose triggers match ``text`` and whose body hasn't been disclosed yet."""
        hits = [s for name, s in self._skills.items() if name not in self._loaded and s.matches(text)]
        self._loaded.update(s.name for s in hits)
        return hits


def load_skillset(names: list[str], root: Path | None = None) -> SkillSet:
    """Load the named skills (loudly failing on a missing one — a spec granted it)."""
    root = root or builtin_skills_dir()
    return SkillSet([load_skill(n, root) for n in names])
