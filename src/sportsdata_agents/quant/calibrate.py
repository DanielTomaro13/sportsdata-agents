"""Score exported fixtures for CALIBRATION, not just for profit.

``scoreboard`` and the replay harness answer "did our edges make money": they
filter to markets where the engine beat a book by more than the noise band. This
reads the same exported fixtures and asks the other question — **are the
probabilities right** — across every market that settled, edge or no edge.

Both matter and they disagree in both directions. An engine can be well
calibrated and still lose to the margin, and it can profit while badly
calibrated (right on the few it bet, wrong elsewhere), which looks exactly like
luck until someone measures here.

The headline is the skill score against the de-vigged CLOSING line, because a
Brier score on its own is mostly a statement about how many longshots were in
the sample. Positive skill means the engine carried information the closing
price did not.

Reads the JSONL that ``replay-export`` writes; runs entirely offline.
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from typing import Any

__all__ = ["calibrate_export", "load_replay_fixtures"]


def load_replay_fixtures(path: str | Path) -> tuple[list[Any], dict[str, int]]:
    """Read exported rows into engine ``ReplayFixture`` objects.

    Returns the fixtures and a count of what was dropped and why. A calibration
    run that silently discards rows overstates its sample, which is the same
    rule the exporter itself follows.
    """
    from sportsdata_engines.replay import ReplayFixture

    known = {f.name for f in fields(ReplayFixture)}
    fixtures: list[Any] = []
    dropped: dict[str, int] = {}

    def _drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            _drop("unparseable_json")
            continue
        if not isinstance(row, dict):
            _drop("not_an_object")
            continue
        extra = set(row) - known
        if extra:
            # the exporter is shaped for ReplayFixture, so an unknown key means
            # the two have drifted — worth reporting, not silently discarding
            _drop(f"unknown_fields:{','.join(sorted(extra))}")
        try:
            fixtures.append(ReplayFixture(**{k: v for k, v in row.items() if k in known}))
        except (TypeError, ValueError) as exc:
            _drop(f"malformed:{type(exc).__name__}")
    return fixtures, dropped


def calibrate_export(
    path: str | Path, *, bins: int = 10, min_family_rows: int = 30
) -> dict[str, Any]:
    """Calibration report for an exported replay file."""
    from sportsdata_engines.replay import calibration_report

    fixtures, dropped = load_replay_fixtures(path)
    report = calibration_report(fixtures, bins=bins, min_family_rows=min_family_rows)
    report["fixtures"] = len(fixtures)
    report["dropped"] = dropped
    return report
