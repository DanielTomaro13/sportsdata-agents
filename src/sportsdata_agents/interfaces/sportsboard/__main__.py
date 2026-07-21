import os

import uvicorn

from sportsdata_agents.interfaces.sportsboard.server import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT") or os.environ.get("SPORTSBOARD_PORT") or 8792)
    uvicorn.run(app, host="127.0.0.1", port=port)
