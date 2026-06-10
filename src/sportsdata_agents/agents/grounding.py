"""Grounding verification (§13.1/D26): no number leaves a run unless a tool said it.

Deterministic by design (P8 — no LLM judging an LLM here): extract the numeric claims
from a draft answer and require each to appear in the run's **evidence** — the user's
own message plus every tool result. A figure found nowhere is flagged back to the
model once ("cite fetched data or say data unavailable"); a second failure surfaces as
``verified=False``, honestly.

This is the highest-leverage anti-hallucination control: prose can waffle, but a
number the tools never produced is a fabrication, detectable mechanically.
"""

from __future__ import annotations

import re

# Digits with optional thousands-separators and decimal part: 42 · 1,234 · 2.50 · .75
NUMBER_RE = re.compile(r"(?<![\d.])(?:\d[\d,]*(?:\.\d+)?|\.\d+)")

ADVISORY_DISCLAIMER = "informational only — not betting or financial advice"


def _normalize(token: str) -> str | None:
    """Canonical form for comparison ('1,234'→'1234', '2.50'→'2.5'); None = too trivial.

    Single-digit integers are skipped: '1 sentence', 'top 5' style phrasing flags far
    more noise than fabrication it would ever catch.
    """
    cleaned = token.replace(",", "").rstrip(".")
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if "." not in cleaned and len(cleaned) < 2:
        return None
    if value.is_integer():
        return str(int(value))  # never scientific notation ('%g' breaks 1234567)
    return f"{value:.6f}".rstrip("0").rstrip(".")


def extract_numbers(text: str) -> set[str]:
    """The normalized numeric claims present in ``text``."""
    out: set[str] = set()
    for m in NUMBER_RE.finditer(text):
        norm = _normalize(m.group())
        if norm is not None:
            out.add(norm)
    return out


def grounding_verifier(answer: str, evidence: list[str]) -> tuple[bool, str]:
    """(ok, feedback): every number in the answer must exist in the evidence.

    A number passes if its normalized form matches an evidence number, or appears
    verbatim in the evidence text (covers ids/dates segmented differently).
    """
    claims = extract_numbers(answer)
    if not claims:
        return True, ""
    evidence_text = "\n".join(evidence)
    evidence_numbers = extract_numbers(evidence_text)
    missing = sorted(n for n in claims if n not in evidence_numbers and n not in evidence_text)
    if missing:
        return False, (
            f"these figures appear in no tool result: {missing} — state only numbers "
            f"fetched from tools, or say the data is unavailable"
        )
    return True, ""
