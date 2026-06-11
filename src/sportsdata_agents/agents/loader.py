"""Spec loader + lint (§7).

Discovers ``*.yaml`` agent specs (skipping ``_``-prefixed files, mirroring the MCP
repo's convention), validates them strictly, and cross-checks the loaded set
(duplicate ids, dangling delegation). Errors always carry the file path — a broken
spec must fail loudly and say where.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import yaml
from pydantic import ValidationError

from .outputs import OUTPUT_TYPES
from .skills import builtin_skills_dir as builtin_skills_root
from .spec import AgentSpec, AgentSpecFile


class SpecError(ValueError):
    """A spec failed to parse/validate; the message includes the source path."""

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path


def load_spec_text(text: str, *, source: str = "<string>") -> AgentSpec:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise SpecError(source, f"invalid YAML: {e}") from e
    if not isinstance(data, dict):
        raise SpecError(source, "spec must be a YAML mapping with `spec_version` and `agent`")
    try:
        return AgentSpecFile.model_validate(data).agent
    except ValidationError as e:
        raise SpecError(source, str(e)) from e


def load_spec_file(path: Path) -> AgentSpec:
    return load_spec_text(path.read_text(encoding="utf-8"), source=str(path))


def load_specs_dir(directory: Path) -> dict[str, AgentSpec]:
    """Load every non-underscore ``*.yaml``/``*.yml`` in a directory; duplicate ids are an error."""
    if not directory.is_dir():
        raise SpecError(str(directory), "spec directory does not exist")
    specs: dict[str, AgentSpec] = {}
    sources: dict[str, str] = {}
    paths = sorted(p for pattern in ("*.yaml", "*.yml") for p in directory.glob(pattern))
    for path in paths:
        if path.name.startswith("_"):
            continue
        spec = load_spec_file(path)
        if spec.id in specs:
            raise SpecError(str(path), f"duplicate agent id {spec.id!r} (also defined in {sources[spec.id]})")
        specs[spec.id] = spec
        sources[spec.id] = str(path)
    return specs


def builtin_specs_dir() -> Path:
    # `specs/` is a data directory, not a package — anchor off the parent package.
    return Path(str(resources.files("sportsdata_agents"))) / "specs"


def load_builtin_specs() -> dict[str, AgentSpec]:
    """The specs bundled with the package (user/DB-defined specs merge on top later)."""
    return load_specs_dir(builtin_specs_dir())


def lint_specs(specs: dict[str, AgentSpec]) -> list[str]:
    """Cross-spec checks. Returns problems (empty = clean).

    Per-spec validation already ran in the pydantic models; this catches what only the
    *set* can know — e.g. delegation pointing at an agent that doesn't exist.
    """
    problems: list[str] = []
    for spec in specs.values():
        for target in spec.can_delegate_to:
            if target not in specs:
                problems.append(f"{spec.id}: can_delegate_to {target!r} which is not a loaded agent")
            elif spec.plane == "product" and specs[target].plane == "ops":
                # §3.1: ops agents hold platform creds — no path from customer
                # traffic may reach them, including delegation
                problems.append(
                    f"{spec.id}: a product-plane agent cannot delegate to ops-plane {target!r} (§3.1)"
                )
        if spec.id in spec.can_delegate_to:
            problems.append(f"{spec.id}: an agent cannot delegate to itself")
        if spec.output_type and spec.output_type not in OUTPUT_TYPES:
            problems.append(
                f"{spec.id}: output_type {spec.output_type!r} is not registered "
                f"(known: {sorted(OUTPUT_TYPES)})"
            )
        for skill in spec.skills:
            # Same class of authoring error as an unknown output_type: catch it offline,
            # not at runtime build. (Custom skills_roots are a runtime concern.)
            if not (builtin_skills_root() / skill / "SKILL.md").is_file():
                problems.append(f"{spec.id}: skill {skill!r} not found in the packaged skills")
    problems.extend(_delegation_cycles(specs))
    return problems


def _delegation_cycles(specs: dict[str, AgentSpec]) -> list[str]:
    """Detect delegation cycles (a→b→a): mutual recursion at runtime, held back only
    by step limits — a spec-set bug, not a runtime condition."""
    problems: list[str] = []
    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(specs, WHITE)

    def visit(node: str, path: list[str]) -> None:
        colour[node] = GREY
        for target in specs[node].can_delegate_to:
            if target == node or target not in specs:
                continue  # self/dangling already reported
            if colour[target] == GREY:
                cycle = [*path[path.index(target) :], node, target] if target in path else [node, target]
                problems.append(f"delegation cycle: {' -> '.join(cycle)}")
            elif colour[target] == WHITE:
                visit(target, [*path, target])
        colour[node] = BLACK

    for node in specs:
        if colour[node] == WHITE:
            visit(node, [node])
    return problems
