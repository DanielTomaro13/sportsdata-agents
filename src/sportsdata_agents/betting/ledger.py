"""Every decision the betting plane made, and what happened to it.

Append-only, one JSON object per line. It is the paper trail in `paper` mode and the
audit record in `auto` mode, and both matter for the same reason: the only way to know
whether a policy is any good is to look at what it actually decided, including the bets
it declined.

REFUSALS ARE RECORDED, NOT DISCARDED. A ledger of placements alone answers "did I win",
which is the less useful question. A ledger that also holds "edge 1.8%, below the 3%
floor" answers "is my floor in the right place", which is the one that improves a
policy. Skips are cheap to store and impossible to reconstruct later.

## Why the budget reads from here

`staked_today` and `open_exposure` are derived from the ledger rather than kept in a
counter beside it. A counter and a log can disagree — after a crash between the write
and the increment, they will — and when they disagree about money the log is right. So
there is one source of truth and the arithmetic is done on read.

Only rows that actually reached a bookmaker (`placed`) count toward spend. A `paper` row
is a decision, not a stake, and must never consume real budget: if it did, a week of
paper running would lock out the first real bet.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

#: proposed — the scanner found it and the policy sized it; no money, no human yet
#: paper    — recorded deliberately instead of placed (paper mode)
#: asked    — handed to a human, awaiting an answer
#: placed   — a bookmaker accepted it; this is the only status that spends budget
#: rejected — the book refused it, or the drift gate abandoned it before placing
#: skipped  — policy declined it
Status = Literal["proposed", "paper", "asked", "placed", "rejected", "skipped"]


@dataclass
class Entry:
    """One decision. Written once; a later fate (asked → placed) is a NEW row carrying
    the same `intent_id`, so the file is never rewritten and history is never lost."""

    intent_id: str
    at: str                       # ISO-8601 UTC
    status: Status
    book: str
    reason: str
    #: What the bet was, in the plane's own terms — never free text from a bookmaker.
    legs: list[dict] = field(default_factory=list)
    stake: float = 0.0
    odds: float = 0.0
    edge: float = 0.0
    #: The book's receipt, when it gave one.
    receipt: dict[str, Any] = field(default_factory=dict)

    def spends_budget(self) -> bool:
        """Only real placements consume real money."""
        return self.status == "placed"


class Ledger:
    """A JSONL file. Concurrent appends from one process are safe; the write is a single
    `write()` of one line opened in append mode, which the OS will not interleave."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, entry: Entry) -> Entry:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(entry), sort_keys=True) + "\n")
        return entry

    def __iter__(self) -> Iterator[Entry]:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield Entry(**json.loads(line))
            except (json.JSONDecodeError, TypeError):
                # A half-written or future-shaped row must not take the whole ledger
                # down — and must not silently vanish either, so it is skipped for
                # arithmetic but stays in the file for a human to look at.
                continue

    # ─── the arithmetic the budget depends on ───────────────────────────

    def staked_on(self, day: date) -> float:
        return sum(e.stake for e in self if e.spends_budget() and _day_of(e) == day)

    def bets_on(self, day: date) -> int:
        return sum(1 for e in self if e.spends_budget() and _day_of(e) == day)

    def open_exposure(self) -> float:
        """Everything placed and not yet settled. Settlement is recorded by
        `settle()`, so anything without a settlement row is still at risk."""
        settled = {e.intent_id for e in self if e.status == "rejected"}
        settled |= {e.intent_id for e in self if e.receipt.get("settled")}
        return sum(e.stake for e in self if e.spends_budget() and e.intent_id not in settled)

    def by_intent(self, intent_id: str) -> list[Entry]:
        return [e for e in self if e.intent_id == intent_id]

    def latest(self, intent_id: str) -> Entry | None:
        rows = self.by_intent(intent_id)
        return rows[-1] if rows else None


def _day_of(entry: Entry) -> date:
    return datetime.fromisoformat(entry.at).astimezone(UTC).date()


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def atomic_write(path: Path, text: str) -> None:
    """Replace a file's contents without a window where it is truncated — used for the
    policy file, which a crash mid-write would otherwise leave unparseable and thereby
    fail open on the next run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
