"""Sports board — a Bloomberg-style terminal for team sports where the
prediction markets + exchange (Kalshi · Polymarket · Betfair · Pinnacle) form
the SHARP line and the bookmakers are measured against it for value, with an
engine-powered same-game-multi price generator.

Warehouse-backed (reads the pipeline the ingest already fills). Run with:

    python -m sportsdata_agents.interfaces.sportsboard   # http://127.0.0.1:8792
"""
