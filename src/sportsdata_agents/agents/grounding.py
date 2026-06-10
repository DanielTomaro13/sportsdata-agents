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


# Scales accepted between claim and evidence: identity, percent (0.5 → "50%"),
# and its inverse ("0.5%" of something stated as a ratio).
_SCALES = (1.0, 100.0, 0.01)


def _claim_matches(claim: str, evidence_numbers: set[str], evidence_text: str) -> bool:
    """Exact/verbatim match, else tolerance match: an evidence number rounded to the
    CLAIM's precision (optionally percent-scaled) equals it. Models legitimately round
    tool figures (2.0526 → "≈2.05") and percent-convert probabilities (0.5 → "50%");
    flagging those burns the retry on honest answers. Fabrications stay caught —
    58 cannot round to 62 at any scale."""
    if claim in evidence_numbers:
        return True
    # Verbatim form in the raw text — BOUNDARY-GUARDED, never bare substring: a
    # fabricated "42" must not verify because "15423" appears in some id/figure.
    if re.search(rf"(?<![\d.]){re.escape(claim)}(?![\d.])", evidence_text):
        return True
    value = float(claim)
    decimals = len(claim.split(".")[1]) if "." in claim else 0
    for ev in evidence_numbers:
        for scale in _SCALES:
            if abs(round(float(ev) * scale, decimals) - value) < 1e-9:
                return True
    return False


def grounding_verifier(answer: str, evidence: list[str]) -> tuple[bool, str]:
    """(ok, feedback): every number in the answer must exist in the evidence
    (exactly, verbatim, or as a rounded/percent-scaled form of an evidence figure)."""
    claims = extract_numbers(answer)
    if not claims:
        return True, ""
    evidence_text = "\n".join(evidence)
    evidence_numbers = extract_numbers(evidence_text)
    missing = sorted(n for n in claims if not _claim_matches(n, evidence_numbers, evidence_text))
    if missing:
        return False, (
            f"these figures appear in no tool result: {missing} — state only numbers "
            f"fetched from tools, or say the data is unavailable"
        )
    return True, ""
