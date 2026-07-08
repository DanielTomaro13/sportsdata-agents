"""Run the RacingBoard terminal: python -m sportsdata_agents.interfaces.racingboard

A Bloomberg-style live racing money-flow terminal (Thoroughbred / Greyhound /
Harness) — tote pool-share momentum, a live pick per race, de-vigged fair
price and the value edge of the best book price, across TAB + Sportsbet +
Pointsbet + Betfair. Vendored from github.com/DanielTomaro13/RacingBoard; the
data layer reuses this stack's sibling sportsdata-mcp engine.
"""

import os

import uvicorn

from sportsdata_agents.interfaces.racingboard.config import settings
from sportsdata_agents.interfaces.racingboard.server import app

if __name__ == "__main__":
    # default to 8791 (beside the moneyflow board on 8787) unless a port is set
    port = int(os.environ.get("PORT") or os.environ.get("MF_PORT") or 8791)
    uvicorn.run(app, host=settings.host, port=port)
