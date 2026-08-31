"""FastAPI app: serves the dashboard, REST snapshots, and a live WebSocket feed."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
import hashlib

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


# PUBLISHING THE ENGINE PRICE IS DELIBERATE. Operator's decision, 1 Sep 2026.
#
# RunnerFlow.to_dict() is asdict(), so `engine_prob` and `fair_source` go out
# on the public API at live.sportsdata-ai.com along with everything else. That
# is intended, not an oversight: the board shows the engine's opinion next to
# the books, and it shows it to everyone.
#
# This note exists because the shape of it looks exactly like a leak, and it
# WAS briefly gated behind a CF-Connecting-IP check on the strength of that
# resemblance. If you are reading this because you just noticed a model's win
# probabilities on a public endpoint: it is on purpose, ask before removing it.
#
# What is NOT public, and must stay that way, is the engine CODE. The
# sportsdata_engines package is absent from every board venv by design and
# lives only in /opt/racing-engine and /opt/ledger, neither of which serves
# anything. Publishing an output is a choice; publishing the model is not.


@app.get("/api/board")
async def api_board() -> JSONResponse:
    # The board summary carries no per-runner engine field today, but it is
    # built from the same dataclasses, so it goes through the same door.
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


def _asset_version() -> str:
    """A short hash of the frontend files, for cache-busting their URLs.

    Headers alone are not enough here. The origin sends Cache-Control: no-cache,
    but Cloudflare sits in front of the public board and replaces it with
    max-age=14400 -- so a browser is told to keep app.js for four hours and a
    normal refresh will not beat that. A deploy then lands correctly on the server
    and the board keeps rendering the old frontend, which is exactly what happened
    to the Ladbrokes label.

    Versioning the URL sidesteps every cache in the path, because a changed file is
    simply a different URL. Computed once per process, at import.
    """
    h = hashlib.sha256()
    for name in ("app.js", "styles.css", "config.js"):
        f = STATIC_DIR / name
        if f.exists():
            h.update(f.read_bytes())
    return h.hexdigest()[:10]


ASSET_V = _asset_version()


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
async def index() -> Response:
    """index.html with versioned asset URLs. Mounted before StaticFiles so it wins."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "styles.css", "config.js"):
        html = html.replace(f'"{name}"', f'"{name}?v={ASSET_V}"')
    return Response(html, media_type="text/html",
                    headers={"Cache-Control": "no-cache"})


class _RevalidatingStatic(StaticFiles):
    """StaticFiles that asks the browser to check before reusing a file.

    The default sends an ETag and Last-Modified but no Cache-Control, so a browser
    is free to reuse app.js from disk for as long as it likes without asking --
    and Cloudflare, in front of the public board, does the same. A frontend change
    then ships to the server and reaches nobody: the board kept rendering the old
    book label for a deploy that had already landed correctly.

    `no-cache` does not mean "do not cache". It means "cache it, but revalidate
    before use", which is exactly right for a dashboard: the ETag turns an
    unchanged file into a 304 costing no bytes, and a changed one arrives at once.
    """

    def file_response(self, *args, **kwargs):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


# Serve the frontend at root so asset paths (styles.css / app.js / config.js /
# data/replay.json) resolve identically here and on GitHub Pages. Mounted LAST so
# the explicit /api and /ws routes above still win.
app.mount("/", _RevalidatingStatic(directory=str(STATIC_DIR), html=True), name="static")
