"""Typed agent outputs (§7 `output_type`): registered pydantic result schemas.

A spec naming an `output_type` makes the harness instruct the model to answer in that
JSON shape and validate the final answer against it (one format-feedback retry). Typed
results make specialist answers machine-consumable — the orchestrator and (later) the
UI chain on fields, not prose. Portable by construction: plain JSON-in-text, no
vendor-specific structured-output APIs.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field, ValidationError

# ─── result schemas ───────────────────────────────────────────────────────


class PriceQuote(BaseModel):
    book: str
    odds: float
    fetched_at: str | None = None


class OddsComparison(BaseModel):
    """The odds specialist's result: one selection priced across books."""

    selection: str
    quotes: list[PriceQuote] = Field(default_factory=list)
    best: PriceQuote
    fair_probability: float | None = None
    commentary: str = ""
    sources: list[str] = Field(default_factory=list)


class Fact(BaseModel):
    claim: str
    value: str
    source: str  # provider/tool the figure came from


class StatsAnswer(BaseModel):
    """The stats specialist's result: an answer grounded in sourced facts."""

    answer: str
    facts: list[Fact] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


OUTPUT_TYPES: dict[str, type[BaseModel]] = {
    "OddsComparison": OddsComparison,
    "StatsAnswer": StatsAnswer,
}


def get_output_type(name: str) -> type[BaseModel]:
    """Resolve a registered output type; a spec naming an unknown one fails loudly."""
    if name not in OUTPUT_TYPES:
        raise KeyError(f"unknown output_type {name!r}; registered: {sorted(OUTPUT_TYPES)}")
    return OUTPUT_TYPES[name]


# ─── parsing ──────────────────────────────────────────────────────────────

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> str:
    """Best-effort extraction of a JSON object from model text (fences, surrounding prose)."""
    m = _FENCE.search(text)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text.strip()


def parse_output(text: str, model: type[BaseModel]) -> tuple[BaseModel | None, str]:
    """(instance, "") on success; (None, error-feedback) on failure."""
    try:
        return model.model_validate_json(extract_json(text)), ""
    except (ValidationError, json.JSONDecodeError, ValueError) as e:
        return None, str(e)


def schema_instructions(model: type[BaseModel]) -> str:
    """The system-prompt suffix that asks for the typed shape."""
    return (
        "\n\nWhen you give your FINAL answer (not tool calls), respond ONLY with a JSON object "
        f"matching this schema — no prose outside the JSON:\n{json.dumps(model.model_json_schema())}"
    )
