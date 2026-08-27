"""Does the market dictionary still map what the books are actually sending?

The dictionary renames each book's market names onto shared family keys, and everything
cross-book depends on it: two books only compare when both their names land on the same
family. When a book renames a market, its rows quietly stop mapping and simply drop out
of every comparison. Nothing errors. The board just gets thinner, which looks like a
quiet day rather than a regression.

Measured 2026-08-27: **10% of stored market rows map to a family; 90% flow through
book-native.** Much of that 90% is legitimately book-local (Kalshi product tickers,
alternate lines that must NOT merge into base families), but a large mappable tail is not
— racing `win`/`place` at Betfair and Dabble, Unibet's `h2h - win`, thousands of rows
each. So this module measures rather than assumes.

## Two drift questions, and only one runs in CI

**Did WE break it?** An alias that used to be in the dictionary is gone — a bad edit, a
botched merge, an OTA overlay that dropped entries. Pure code-and-data: no warehouse
needed, so it is a unit test and it gates every commit. `check_dictionary_regression`.

**Did THEY change it?** A market name that used to arrive mapped now arrives unmapped,
because the book renamed it. This can only be seen against real captured rows, so it
needs the warehouse and runs as an operator/nightly check, never in CI.
`measure_coverage` + `compare_coverage`.

Keeping them apart matters: a CI job that needs a populated warehouse either gets skipped
into uselessness or fails for reasons that have nothing to do with the commit.

## Why coverage is per book, and a floor rather than a target

Books differ enormously in how much of their catalogue is mappable — an exchange listing
`win`/`place` on every race is mostly mappable; a prediction market keyed by product
ticker mostly is not. One global percentage would be dominated by whichever book captured
most rows that day and would move for reasons that are not drift.

So the gate is per book, and it is a FLOOR against that book's own recorded baseline, not
an absolute target. The question is never "is 60% good" — it is "did this book's coverage
fall off a cliff since we last looked", which is what a rename looks like.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: How far a book's coverage may fall below its baseline before it is drift rather than
#: noise. Coverage moves a little on its own as the mix of captured competitions shifts,
#: so a floor with no tolerance would cry wolf; a rename drops coverage far further than
#: this, because it takes every row of that market with it.
COVERAGE_TOLERANCE = 0.10

#: A book-native name is only worth reporting once it carries real volume. Below this it
#: is a long-tail novelty market, and a report full of those is a report nobody reads.
MIN_ROWS_TO_REPORT = 500

BASELINE_PATH = Path(__file__).with_name("dictionary_baseline.json")


@dataclass
class BookCoverage:
    book: str
    mapped_rows: int
    unmapped_rows: int

    @property
    def total(self) -> int:
        return self.mapped_rows + self.unmapped_rows

    @property
    def coverage(self) -> float:
        return self.mapped_rows / self.total if self.total else 0.0


@dataclass
class Finding:
    kind: str        # "coverage_drop" | "alias_lost" | "new_unmapped"
    book: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.kind}] {self.book}: {self.detail}"


@dataclass
class CoverageReport:
    books: dict[str, BookCoverage] = field(default_factory=dict)
    #: (book, book-native market name) → row count, for the names worth a human's time.
    unmapped: dict[tuple[str, str], int] = field(default_factory=dict)

    def to_baseline(self) -> dict[str, Any]:
        return {
            "coverage": {b: round(c.coverage, 4) for b, c in sorted(self.books.items())},
            "rows": {b: c.total for b, c in sorted(self.books.items())},
        }


# ─── did WE break it: dictionary regression (no warehouse) ──────────────


def current_aliases() -> dict[str, str]:
    """alias → family, as THIS MACHINE resolves it (seed + OTA + local overrides).

    Machine-specific by construction: `market_dictionary.local.json` is the steward's
    working file and exists nowhere else. Right for the operator-machine drift report,
    wrong for a baseline — see `packaged_aliases`.
    """
    from sportsdata_agents.operations.ingestion.normalizers import _dictionary

    return dict(_dictionary()["markets"])


def packaged_aliases() -> dict[str, str]:
    """alias → family from the SHIPPED dictionary only — no local overrides.

    The baseline has to be built from something every environment shares. Built from
    `current_aliases()` it captured 14 aliases that existed only on the machine that
    generated it, so the guard reported them "lost" everywhere else and failed from the
    day it was added — including in CI, which is the one place it was meant to run.

    A guard measured against machine-local state cannot pass anywhere else, and a guard
    that never passes gets ignored rather than fixed.
    """
    import json

    from sportsdata_agents.operations.datafeed import data_text
    from sportsdata_agents.operations.ingestion.normalizers import _norm_name

    seed = json.loads(data_text("market_dictionary"))
    # Same shape the runtime builds: family -> [aliases] inverted, names normalised the
    # way canonical_market() will normalise a book's market before looking it up.
    return {
        _norm_name(alias): family
        for family, aliases in (seed.get("markets") or {}).items()
        for alias in aliases
    }


def check_dictionary_regression(
    baseline_aliases: dict[str, str],
    current: dict[str, str] | None = None,
) -> list[Finding]:
    """Every alias the baseline knew must still map, and to the SAME family.

    Additions are fine and expected — the steward's whole job is adding them. What is
    never fine is an alias disappearing or being re-pointed, because the rows that used
    to join across books silently stop joining, and the only visible symptom is a
    thinner board.
    """
    current = current if current is not None else current_aliases()
    out: list[Finding] = []
    for alias, family in sorted(baseline_aliases.items()):
        now = current.get(alias)
        if now is None:
            out.append(Finding("alias_lost", "-", f"{alias!r} no longer maps (was {family!r})"))
        elif now != family:
            out.append(Finding(
                "alias_lost", "-",
                f"{alias!r} moved family: {family!r} → {now!r}. If deliberate, re-baseline; "
                f"a silent re-point changes what joins what.",
            ))
    return out


# ─── did THEY change it: live coverage (needs the warehouse) ────────────


async def measure_coverage(session: Any, *, min_rows: int = MIN_ROWS_TO_REPORT) -> CoverageReport:
    """Per-book mapped/unmapped row counts from the odds warehouse.

    A stored `market` value is either a family key (it mapped at ingest) or the book's
    own name (it did not) — the normalizers write the family when they recognise the
    name and pass the original through when they do not, so the stored value alone says
    which happened.
    """
    from sqlalchemy import func, select

    from sportsdata_agents.data.models import OddsSnapshot

    families = set(current_aliases().values())
    rows = (await session.execute(
        select(OddsSnapshot.book, OddsSnapshot.market, func.count())
        .group_by(OddsSnapshot.book, OddsSnapshot.market)
    )).all()

    report = CoverageReport()
    for book, market, count in rows:
        book = book or "?"
        cov = report.books.setdefault(book, BookCoverage(book, 0, 0))
        if market in families:
            cov.mapped_rows += count
        else:
            cov.unmapped_rows += count
            if count >= min_rows:
                report.unmapped[(book, market)] = count
    return report


def compare_coverage(
    current: CoverageReport,
    baseline: dict[str, Any],
    *,
    tolerance: float = COVERAGE_TOLERANCE,
) -> list[Finding]:
    """Coverage that fell off a cliff, and high-volume names that are newly unmapped."""
    out: list[Finding] = []
    base_cov = baseline.get("coverage") or {}

    for book, cov in sorted(current.books.items()):
        was = base_cov.get(book)
        if was is None:
            continue  # a book we have never baselined is not drift; re-baseline to adopt it
        if cov.coverage < was - tolerance:
            out.append(Finding(
                "coverage_drop", book,
                f"{cov.coverage:.0%} of {cov.total} rows map, was {was:.0%} — "
                f"a drop this size is a renamed market, not mix shift",
            ))

    known = set(baseline.get("known_unmapped") or [])
    for (book, market), count in sorted(current.unmapped.items(), key=lambda kv: -kv[1]):
        if market not in known:
            out.append(Finding(
                "new_unmapped", book,
                f"{market!r} arrived {count} times and maps to nothing "
                f"(either alias it, or add it to known_unmapped if it is book-local)",
            ))
    return out


def load_baseline(path: Path | None = None) -> dict[str, Any]:
    p = path or BASELINE_PATH
    return json.loads(p.read_text()) if p.exists() else {}


def save_baseline(data: dict[str, Any], path: Path | None = None) -> None:
    p = path or BASELINE_PATH
    p.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
