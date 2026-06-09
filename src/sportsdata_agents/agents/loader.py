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
    """Load every non-underscore ``*.yaml`` in a directory; duplicate ids are an error."""
    specs: dict[str, AgentSpec] = {}
    sources: dict[str, str] = {}
    for path in sorted(directory.glob("*.yaml")):
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
        if spec.id in spec.can_delegate_to:
            problems.append(f"{spec.id}: an agent cannot delegate to itself")
    return problems
