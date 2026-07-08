"""Live Betfair money-flow board for AU/NZ racing.

A standalone read-only web UI: pick an upcoming race, watch where the
exchange money is going runner by runner (traded volume deltas, price
moves, book pressure) in real time. Talks straight to Betfair's public
readonly endpoints — no warehouse coupling, works even when ingest is
idle. Run with:

    python -m sportsdata_agents.interfaces.moneyflow  # http://127.0.0.1:8787
"""
