"""FastAPI app: serves the dashboard, REST snapshots, and a live WebSocket feed."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .poller import Poller
from .store import Store

STATIC_DIR = Path(__file__).parent / "static"


class Hub:
    """Fan-out of poller updates to all connected WebSocket clients."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def add(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def remove(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, message: dict) -> None:
        data = json.dumps(message, default=str)
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.remove(ws)


store = Store()
hub = Hub()
poller = Poller(store, broadcast=hub.broadcast)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(poller.start())
    try:
        yield
    finally:
        await poller.stop()
        task.cancel()


app = FastAPI(title="Racing Money Flow", lifespan=lifespan)

# Allow a statically-hosted page (e.g. GitHub Pages) to call this backend when
# it's deployed separately and pointed here via ?api=.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True, "races": len(store.races)})


@app.get("/api/board")
async def api_board() -> JSONResponse:
    return JSONResponse({"board": store.board(), "movers": store.movers()})


@app.get("/api/race/{race_key:path}")
async def api_race(race_key: str) -> JSONResponse:
    detail = store.race_detail(race_key)
    if detail is None:
        return JSONResponse({"error": "not found or not yet polled"}, status_code=404)
    return JSONResponse(detail)


def _win_probs_for(race_key: str) -> tuple[dict[int, float], str]:
    """Win probabilities for a race from the latest snapshot, and their source.

    Uses each active runner's fair_price (1/fair) — which finalize_snapshot
    already sets from the racing ENGINE when it covers the field, else
    Betfair/tote — so exotics inherit the engine's opinion automatically."""
    st = store.races.get(race_key)
    if st is None or st.latest is None:
        return {}, "none"
    probs: dict[int, float] = {}
    sources: set[str] = set()
    for r in st.latest.runners:
        if r.scratched or not r.fair_price or r.fair_price <= 1.0:
            continue
        probs[int(r.number)] = 1.0 / float(r.fair_price)
        if r.fair_source:
            sources.add(r.fair_source)
    source = "engine" if sources == {"engine"} else (
        "+".join(sorted(sources)) if sources else "market")
    return probs, source


@app.post("/api/price")
async def api_price(body: dict) -> JSONResponse:
    """Generate a fair price for an exotic or same-race multi on a race's
    live win probabilities. Body:
      {"race_key": ..., "bet": "exacta|quinella|trifecta|first4|srm",
       "selection": [n, ...], "legs": [{"runner": n, "position": "top3"}],
       "box": bool, "margin": 0.0}"""
    from sportsdata_agents.quant.exotics import price_exotic, price_srm

    race_key = str(body.get("race_key", ""))
    bet = str(body.get("bet", "")).lower()
    probs, source = _win_probs_for(race_key)
    if not probs:
        return JSONResponse({"warning": "race not priced yet"}, status_code=409)
    margin = float(body.get("margin") or 0.0)
    if bet == "srm":
        result = price_srm(probs, list(body.get("legs") or []), margin=margin)
    elif bet in ("exacta", "quinella", "trifecta", "first4"):
        result = price_exotic(probs, bet, [int(x) for x in body.get("selection") or []],
                              box=bool(body.get("box")), margin=margin)
    else:
        return JSONResponse({"warning": f"unknown bet {bet!r}"}, status_code=400)
    result["price_source"] = source
    return JSONResponse(result)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    await hub.add(ws)
    # Send an immediate snapshot so a fresh client isn't blank until next tick.
    await ws.send_text(json.dumps(
        {"type": "board", "board": store.board(), "movers": store.movers()},
        default=str,
    ))
    try:
        while True:
            # Client may request a specific race's detail on demand.
            msg = await ws.receive_text()
            try:
                req = json.loads(msg)
            except json.JSONDecodeError:
                continue
            if req.get("type") == "subscribe" and req.get("race_key"):
                detail = store.race_detail(req["race_key"])
                if detail:
                    await ws.send_text(json.dumps(
                        {"type": "race", "race_key": req["race_key"], "detail": detail},
                        default=str,
                    ))
    except WebSocketDisconnect:
        await hub.remove(ws)
    except Exception:
        await hub.remove(ws)


# Serve the frontend at root so asset paths (styles.css / app.js / config.js /
# data/replay.json) resolve identically here and on GitHub Pages. Mounted LAST so
# the explicit /api and /ws routes above still win.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
