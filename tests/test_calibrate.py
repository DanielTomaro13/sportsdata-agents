"""Calibration over an exported replay — the other half of the scoreboard.

`scoreboard` asks whether our edges made money, on a filtered subset. This asks
whether the probabilities are right, across everything that settled. The tests
here are about the SEAM: that exported rows load into engine fixtures, that
nothing is dropped silently, and that the numbers come out where a known answer
says they should.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from sportsdata_agents.quant.calibrate import calibrate_export, load_replay_fixtures

# the pricing engine is an OPTIONAL dependency — the platform runs bare by
# default (see test_engines_seam), so these tests skip rather than fail when it
# is not installed. Everything here needs a real board, not a stub: the point is
# that engine probabilities land on the diagonal.
pytest.importorskip("sportsdata_engines")


def _export(tmp_path: Path, n: int = 12, *, benchmark: str = "model",
            pin_levers: bool = False) -> Path:
    """Fixtures whose RESULT is drawn from the engine's own distribution.

    The labels then come from the same process that prices them, so a correct
    pipeline must report near-perfect calibration — which makes this a test of
    the seam rather than of the engine's accuracy.

    ``pin_levers`` writes the levers into the row so the replay prices from them
    directly. Without it the row carries only ``quotes`` — the shape the real
    exporter emits — and the engine REFITS levers from those quotes, landing a
    hair away from the ones that generated the labels. That difference is real
    and belongs in most of these tests; it is switched off only where a test's
    premise is that the benchmark and the model are the same number.
    """
    from sportsdata_engines import afl
    from sportsdata_engines.core.types import FixtureInputs
    from sportsdata_engines.footy.spine import simulate_scores

    rng = np.random.default_rng(4)
    rows: list[dict[str, Any]] = []
    for i in range(n):
        margin = float(rng.normal(0.0, 14.0))
        total = float(rng.normal(168.0, 12.0))
        home, away = simulate_scores(afl.PROFILE, margin, total, f"afl:X{i}:board")
        pick = int(rng.integers(len(home)))
        board = afl.price_board(FixtureInputs(
            sport="afl", fixture_id=f"X{i}",
            levers={"expected_margin": margin, "expected_total": total}))
        h2h = {p.selection: p.fair_probability for p in board if p.market == "h2h"}
        line = next(p for p in board if p.market == "total").line

        close = []
        for selection, probability in h2h.items():
            q = probability if benchmark == "model" else 0.5
            close.append({"market": "h2h", "selection": selection,
                          "line": None, "odds": 1.0 / q})
        row: dict[str, Any] = {
            "sport": "afl", "fixture_id": f"X{i}",
            "quotes": {"h2h": [1.0 / h2h["home"], 1.0 / h2h["away"]],
                       "total": [line, 1.9, 1.9]},
            "taken_quotes": [], "close_quotes": close,
            "result": {"home_score": int(home[pick]), "away_score": int(away[pick])},
        }
        if pin_levers:
            row["levers"] = {"expected_margin": margin, "expected_total": total}
        rows.append(row)

    path = tmp_path / "replay-fixtures.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def test_exported_rows_load_straight_into_engine_fixtures(tmp_path: Path) -> None:
    """The exporter is shaped for ReplayFixture kwargs. If that ever drifts,
    this is where it should show up."""
    fixtures, dropped = load_replay_fixtures(_export(tmp_path, 4))
    assert len(fixtures) == 4
    assert dropped == {}
    assert {f.sport for f in fixtures} == {"afl"}


def test_nothing_is_dropped_silently(tmp_path: Path) -> None:
    """A calibration run that discards rows without saying so overstates its
    sample — the same rule the exporter follows for skipped fixtures."""
    path = tmp_path / "messy.jsonl"
    path.write_text(
        "\n".join([
            "not json at all",
            json.dumps([1, 2, 3]),
            json.dumps({"sport": "afl", "fixture_id": "A", "quotes": {},
                        "taken_quotes": [], "result": {}, "surprise": 1}),
            "",
        ]) + "\n", encoding="utf-8")
    fixtures, dropped = load_replay_fixtures(path)
    assert dropped["unparseable_json"] == 1
    assert dropped["not_an_object"] == 1
    # an unknown key is reported by NAME so the drift is identifiable
    assert any(k.startswith("unknown_fields:surprise") for k in dropped)
    # ...and the row still loads on the fields that are known
    assert len(fixtures) == 1


def test_calibration_over_engine_generated_labels_lands_on_the_diagonal(
    tmp_path: Path,
) -> None:
    report = calibrate_export(_export(tmp_path, 24))
    assert report["fixtures"] == 24
    assert report["rows"] > 1000
    off = [
        b for b in report["reliability"]
        if b["n"] >= 100
        and abs(b["predicted"] - b["observed"]) > 4.0 * max(b["std_error"], 1e-9)
    ]
    assert not off, f"buckets off the diagonal: {off}"


def test_a_benchmark_equal_to_the_model_scores_no_skill(tmp_path: Path) -> None:
    """Closing prices taken from the model itself must score ~0 skill — but only
    when the replay prices from the SAME levers. Hence `pin_levers`: with only
    quotes on the row the engine refits, and the refit is a different (slightly)
    number, so a non-zero skill there is honest rather than a bug."""
    report = calibrate_export(_export(tmp_path, 16, benchmark="model",
                                      pin_levers=True))
    assert report["skill_vs_close"] == pytest.approx(0.0, abs=1e-9)
    assert report["benchmarked_rows"] > 0


def test_refitting_from_quotes_moves_the_price_off_the_benchmark(
    tmp_path: Path,
) -> None:
    """The production path: the row carries quotes, so the engine refits levers
    rather than being handed them. The result is a small but real disagreement
    with a benchmark built from the original levers — worth pinning, because a
    reader seeing non-zero skill on identical inputs should know why."""
    pinned = calibrate_export(_export(tmp_path, 16, benchmark="model",
                                      pin_levers=True))
    refit = calibrate_export(_export(tmp_path, 16, benchmark="model"))
    assert pinned["skill_vs_close"] == pytest.approx(0.0, abs=1e-9)
    assert abs(refit["skill_vs_close"]) > 1e-6
    assert abs(refit["skill_vs_close"]) < 0.1, "a refit should be close, not different"


def test_an_uninformative_benchmark_scores_positive_skill(tmp_path: Path) -> None:
    """The engine should beat a coin flip. If it ever does not, the headline
    number has stopped meaning anything."""
    report = calibrate_export(_export(tmp_path, 24, benchmark="coinflip"))
    assert report["skill_vs_close"] is not None
    assert report["skill_vs_close"] > 0.0


def test_families_below_the_floor_are_named_not_scored(tmp_path: Path) -> None:
    report = calibrate_export(_export(tmp_path, 12), min_family_rows=200)
    assert report["thin_families"]
    for family in report["thin_families"]:
        assert family not in report["families"]
    for entry in report["families"].values():
        assert entry["n"] >= 200


def test_an_empty_export_reports_zero_rather_than_raising(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    report = calibrate_export(path)
    assert report["fixtures"] == 0
    assert report["rows"] == 0
