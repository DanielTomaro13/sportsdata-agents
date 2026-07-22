#!/usr/bin/env python3
"""Capture an animated REPLAY of the sports board for the static (GitHub Pages) demo.

The board is warehouse-backed, but GitHub Pages can't run a database. So we sweep
the reader across the trailing time window and emit a sequence of frames the
frontend animates when there's no live backend. Frame shape matches app.js::

    [ { "games": [...], "details": { fixture_id: detail, ... } }, ... ]

Each frame is the board as it looked at that moment — prices, the de-vigged sharp
line, money-flow and Betfair matched all reconstructed from odds history at that
``now`` — so the static page visibly moves, mirroring the racing board's replay.

Usage::

    python -m sportsdata_agents.interfaces.sportsboard.capture_replay \
        [frames] [span_minutes] [out_path]
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

from sportsdata_agents.config import get_settings
from sportsdata_agents.data.db import ensure_schema, make_engine, make_sessionmaker
from sportsdata_agents.interfaces.sportsboard.warehouse import game_detail, list_games


async def capture(n_frames: int, span_minutes: float, out: Path) -> None:
    """Sweep ``now`` over the trailing ``span_minutes`` and write ``n_frames`` frames."""
    engine = make_engine(get_settings().database_url)
    await ensure_schema(engine)
    sessionmaker = make_sessionmaker(engine)
    now = dt.datetime.now(dt.UTC)
    span = dt.timedelta(minutes=span_minutes)
    denom = max(n_frames - 1, 1)

    frames: list[dict[str, Any]] = []
    async with sessionmaker() as session:
        for i in range(n_frames):
            at = now - span + span * (i / denom)
            games = await list_games(session, now=at)
            details: dict[str, Any] = {}
            for g in games:
                detail = await game_detail(session, g["fixture_id"], now=at)
                if detail and not detail.get("error"):
                    details[g["fixture_id"]] = detail
            frames.append({"games": games, "details": details})
            print(f"frame {i + 1}/{n_frames} @ {at:%H:%M}: {len(games)} games, {len(details)} details")
    await engine.dispose()

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(frames, separators=(",", ":"), default=str))
    print(f"wrote {len(frames)} frames -> {out} ({out.stat().st_size / 1024:.0f} KB)")


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 18
    span = float(sys.argv[2]) if len(sys.argv) > 2 else 90.0
    default_out = Path(__file__).resolve().parent / "static" / "data" / "replay.json"
    out = Path(sys.argv[3]) if len(sys.argv) > 3 else default_out
    asyncio.run(capture(n, span, out))


if __name__ == "__main__":
    main()
