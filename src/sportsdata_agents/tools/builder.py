"""Agent-builder tools (M3.3, §7.1): NL → a validated, VERSIONED agent spec.

The user never sees YAML or raw tool names — the builder agent narrates in plain
language and these tools do the wiring:

- ``list_capabilities``: the curated catalogue (capability tags as friendly
  labels, skills, tiers) — what a user may pick from, never the raw tool surface.
- ``draft_agent_spec``: validate a draft against the REAL spec models + lint —
  every guardrail (no-money invariant, plane, semver, unknown skills) applies at
  draft time, not save time.
- ``save_agent_spec``: write to the user specs directory with D27 versioning —
  saving over an existing id requires a version bump and archives the old file as
  ``{id}@{version}.yaml`` so pinned workspaces keep working.

Guardrails by construction (§7.1): user-built specs are always product-plane,
cannot collide with builtin agent ids, and budgets clamp to the spec defaults.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

from sportsdata_agents.agents.harness import ToolDef

BUILDER_TOOL_NAMES = {"list_capabilities", "draft_agent_spec", "save_agent_spec"}

_TIER_LABELS = {"fast": "Fast", "balanced": "Balanced", "strong": "Smart"}


def user_specs_dir() -> Path:
    override = os.environ.get("SPORTSDATA_AGENTS_USER_SPECS_DIR")
    if override:
        root = Path(override)
        root.mkdir(parents=True, exist_ok=True)
        return root
    from sportsdata_agents.paths import specs_dir

    return specs_dir()


def capability_labels() -> dict[str, dict[str, str]]:
    # The OTA overlay (if applied via `agents update-data`) wins over the packaged
    # default, so new capability labels can ship without a full app release.
    from sportsdata_agents.operations.datafeed import data_text

    return json.loads(data_text("capability_labels"))


def builder_tools() -> list[ToolDef]:
    async def list_capabilities(args: dict[str, Any]) -> Any:
        """The curated building-block catalogue: data (capability tags with friendly
        labels), skills, and model tiers. Users pick from THESE — never raw tools."""
        from sportsdata_agents.agents.skills import builtin_skills_dir

        labels = capability_labels()
        skills = []
        for path in sorted(builtin_skills_dir().iterdir()):
            doc = path / "SKILL.md"
            if doc.is_file():
                first = doc.read_text(encoding="utf-8").lstrip("# ").splitlines()[0]
                skills.append({"id": path.name, "summary": first})
        return {
            "data": [{"tag": tag, **info} for tag, info in sorted(labels.items())],
            "skills": skills,
            "tiers": [{"tier": k, "label": v} for k, v in _TIER_LABELS.items()],
            "note": "draft_agent_spec validates everything — guardrails apply at draft time",
        }

    async def draft_agent_spec(args: dict[str, Any]) -> Any:
        """{spec: {id, display_name, goal_prompt, capabilities?, skills?, tier?,
        limits?}} → a validated draft. Returns problems to fix, or the validated
        summary. Drafts are ALWAYS product-plane."""
        from sportsdata_agents.agents.loader import lint_specs, load_builtin_specs, load_spec_text

        draft = dict(args.get("spec") or {})
        agent = {
            "id": str(draft.get("id", "")),
            "display_name": str(draft.get("display_name", "")),
            "description": str(draft.get("description", "")),
            "version": str(draft.get("version", "0.1.0")),
            "plane": "product",  # §7.1: users can never author ops-plane agents
            "model_tier": str(draft.get("tier", "balanced")),
            "system_prompt": str(draft.get("goal_prompt", "")).strip()
            + "\n\nAdvisory only: report and recommend; never imply you can place bets or move money.",
            "tools": {
                "mcp_capabilities": list(draft.get("capabilities") or []),
                "native": list(draft.get("native") or []),
            },
            "skills": list(draft.get("skills") or []),
            "limits": draft.get("limits") or {},
        }
        text = yaml.safe_dump({"spec_version": 1, "agent": agent}, sort_keys=False)
        try:
            spec = load_spec_text(text, source="<draft>")
        except Exception as e:
            return {"ok": False, "problems": [str(e)]}
        builtin = load_builtin_specs()
        if spec.id in builtin:
            return {"ok": False, "problems": [
                f"id {spec.id!r} collides with a builtin agent — pick another name"
            ]}
        problems = lint_specs({**builtin, spec.id: spec})
        problems = [p for p in problems if p.startswith(spec.id)]
        if problems:
            return {"ok": False, "problems": problems}
        return {
            "ok": True,
            "yaml": text,
            "summary": {
                "id": spec.id, "version": spec.version, "tier": spec.model_tier,
                "data": spec.tools.mcp_capabilities, "skills": spec.skills,
                "cost_ceiling_usd": spec.limits.cost_ceiling_usd,
            },
        }

    async def save_agent_spec(args: dict[str, Any]) -> Any:
        """{yaml} → persist a DRAFTED spec to the user specs directory (D27
        versioned: overwriting an id requires a version bump; the old version is
        archived as {id}@{version}.yaml so pinned workspaces keep working)."""
        from sportsdata_agents.agents.loader import load_builtin_specs, load_spec_text

        text = str(args["yaml"])
        spec = load_spec_text(text, source="<save>")
        if spec.plane != "product":
            raise ValueError("user-built agents are product-plane only (§3.1)")
        if spec.id in load_builtin_specs():
            raise ValueError(f"id {spec.id!r} collides with a builtin agent")
        directory = user_specs_dir()
        target = directory / f"{spec.id}.yaml"
        if target.is_file():
            existing = load_spec_text(target.read_text(encoding="utf-8"), source=str(target))
            if existing.version == spec.version:
                raise ValueError(
                    f"{spec.id}@{spec.version} already exists — bump the version to save changes (D27)"
                )
            archive = directory / f"{spec.id}@{existing.version}.yaml"
            archive.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
        target.write_text(text, encoding="utf-8")
        return {"saved": str(target), "id": spec.id, "version": spec.version,
                "note": f'run it with: agents run --agent {spec.id} "..."'}

    def _tool(name: str, fn: Any, props: dict[str, Any], required: list[str]) -> ToolDef:
        return ToolDef(
            name=name,
            description=(fn.__doc__ or name).strip().splitlines()[0],
            parameters={"type": "object", "properties": props, "required": required},
            execute=fn,
        )

    return [
        _tool("list_capabilities", list_capabilities, {}, []),
        _tool("draft_agent_spec", draft_agent_spec, {"spec": {"type": "object"}}, ["spec"]),
        _tool("save_agent_spec", save_agent_spec, {"yaml": {"type": "string"}}, ["yaml"]),
    ]
