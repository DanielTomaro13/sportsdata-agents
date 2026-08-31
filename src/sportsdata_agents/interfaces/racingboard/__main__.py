"""Run the RacingBoard terminal: python -m sportsdata_agents.interfaces.racingboard

A Bloomberg-style live racing money-flow terminal (Thoroughbred / Greyhound /
Harness) — tote pool-share momentum, a live pick per race, de-vigged fair
price and the value edge of the best book price, across TAB + Sportsbet +
Pointsbet + Betfair. Vendored from github.com/DanielTomaro13/RacingBoard; the
data layer reuses this stack's sibling sportsdata-mcp engine.
"""

import asyncio
import os
import sys

import uvicorn

from sportsdata_agents.interfaces.racingboard.config import settings
from sportsdata_agents.interfaces.racingboard.server import app

if __name__ == "__main__":
    if "--coverage" in sys.argv:
        # An audit rather than a server: how much of the board each book reaches, and
        # whether a miss is a catalogue limit or a resolver gap. See coverage.py.
        from sportsdata_agents.interfaces.racingboard.coverage import report

        date = next((a for a in sys.argv[1:] if not a.startswith("-")), None)
        raise SystemExit(0 if asyncio.run(report(date)) == 0 else 0)

    # default to 8791 (beside the moneyflow board on 8787) unless a port is set
    port = int(os.environ.get("PORT") or os.environ.get("MF_PORT") or 8791)
    uvicorn.run(app, host=settings.host, port=port)
