"""The market dictionary must not silently stop mapping what it used to map.

Everything cross-book depends on two books' market names landing on the same family key.
When that breaks, nothing raises: the rows just stop joining and the board gets thinner,
which reads as a quiet day rather than a regression. So it needs a test, because it has
no symptom.

Only the "did WE break it" half lives here — a lost or re-pointed alias is pure code and
data, so it gates every commit. The "did THEY rename it" half needs real captured rows
and runs against the warehouse (`agents dictionary-drift`), never in CI, where an empty
database would make it either skipped or noisy.
"""

from __future__ import annotations

import pytest

from sportsdata_agents.operations.resolution import dictionary_drift as dd

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def baseline():
    b = dd.load_baseline()
    assert b, "dictionary_baseline.json is missing — regenerate it, do not delete it"
    return b


# ─── the guard itself ───────────────────────────────────────────────────


def test_every_baselined_alias_still_maps(baseline) -> None:
    """THE test. An alias that disappears takes every row of that market out of every
    cross-book comparison, and says nothing while doing it."""
    findings = dd.check_dictionary_regression(baseline["aliases"])
    assert findings == [], "\n".join(str(f) for f in findings)


def test_a_lost_alias_is_caught() -> None:
    findings = dd.check_dictionary_regression(
        {"match odds": "h2h", "invented alias that never existed": "h2h"},
        current={"match odds": "h2h"},
    )
    assert len(findings) == 1
    assert findings[0].kind == "alias_lost"
    assert "invented alias" in findings[0].detail


def test_a_re_pointed_alias_is_caught() -> None:
    """Silently moving an alias to another family changes what joins what — as bad as
    losing it, and harder to notice."""
    findings = dd.check_dictionary_regression(
        {"total goals": "total"}, current={"total goals": "h2h"},
    )
    assert len(findings) == 1
    assert "moved family" in findings[0].detail


def test_additions_are_not_drift() -> None:
    """The steward's whole job is adding aliases. Only losses are regressions."""
    findings = dd.check_dictionary_regression(
        {"match odds": "h2h"}, current={"match odds": "h2h", "brand new name": "total"},
    )
    assert findings == []


# ─── the live-coverage half, tested without a warehouse ─────────────────


def test_a_coverage_cliff_is_drift_but_a_wobble_is_not() -> None:
    """Coverage moves on its own as the captured competition mix shifts, so the gate is
    a tolerance band. A rename drops it far further, because it takes every row of that
    market with it."""
    base = {"coverage": {"tab": 0.60}, "known_unmapped": []}

    wobble = dd.CoverageReport(books={"tab": dd.BookCoverage("tab", 550, 450)})   # 55%
    assert dd.compare_coverage(wobble, base) == []

    cliff = dd.CoverageReport(books={"tab": dd.BookCoverage("tab", 200, 800)})    # 20%
    findings = dd.compare_coverage(cliff, base)
    assert [f.kind for f in findings] == ["coverage_drop"]


def test_a_book_never_baselined_is_not_drift() -> None:
    """A newly-captured book has no history to fall from. Adopting it is a re-baseline,
    not a failure."""
    report = dd.CoverageReport(books={"newbook": dd.BookCoverage("newbook", 1, 999)})
    assert dd.compare_coverage(report, {"coverage": {}}) == []


def test_a_high_volume_new_unmapped_name_is_reported() -> None:
    report = dd.CoverageReport(
        books={"tab": dd.BookCoverage("tab", 900, 100)},
        unmapped={("tab", "brand new market"): 4000},
    )
    findings = dd.compare_coverage(report, {"coverage": {"tab": 0.9}, "known_unmapped": []})
    assert [f.kind for f in findings] == ["new_unmapped"]
    assert "brand new market" in findings[0].detail


def test_a_name_we_have_already_judged_book_local_stays_quiet(baseline) -> None:
    """`known_unmapped` is the list of names a human looked at and decided are genuinely
    book-local — Kalshi product tickers, alternate lines that must not merge into base
    families. Without it every run reports the same 389 names and nobody reads it."""
    known = baseline.get("known_unmapped") or []
    assert known, "baseline carries no known_unmapped — every run would be noise"
    report = dd.CoverageReport(
        books={"kalshi": dd.BookCoverage("kalshi", 10, 90)},
        unmapped={("kalshi", known[0]): 9999},
    )
    assert dd.compare_coverage(report, baseline) == []


# ─── what the baseline records ──────────────────────────────────────────


def test_the_baseline_is_real_measurement_not_a_guess(baseline) -> None:
    """Generated from the live warehouse on 2026-08-27, not hand-written. It records a
    POOR state deliberately — coverage as low as 3% on some books — because a regression
    guard freezes wherever you are, and improving it is separate work."""
    assert baseline["coverage"], "no per-book coverage recorded"
    assert baseline["rows"], "no row counts recorded"
    # every book with a coverage figure has the row count that produced it
    assert set(baseline["coverage"]) == set(baseline["rows"])
    assert len(baseline["aliases"]) >= 79
