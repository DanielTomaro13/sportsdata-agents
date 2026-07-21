"""FastAPI app: the sports board over the warehouse (sharp line + book value +
engine SGM price generator)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .warehouse import game_detail, list_games

STATIC_DIR = Path(__file__).parent / "static"
app = FastAPI(title="Sports Board")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["GET", "POST"], allow_headers=["*"])

_sf: async_sessionmaker[AsyncSession] | None = None


def _sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sf
    if _sf is None:
        from sportsdata_agents.config import get_settings
        from sportsdata_agents.data.db import make_engine, make_sessionmaker
        _sf = make_sessionmaker(make_engine(get_settings().database_url))
    return _sf


@app.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.get("/api/games")
async def api_games(hours: float = 12.0) -> JSONResponse:
    async with _sessionmaker()() as s:
        return JSONResponse({"games": await list_games(s, hours=hours)})


@app.get("/api/game/{fixture_id}")
async def api_game(fixture_id: str) -> JSONResponse:
    async with _sessionmaker()() as s:
        detail = await game_detail(s, fixture_id)
    if detail is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(detail)


@app.post("/api/sgm")
async def api_sgm(body: dict) -> JSONResponse:
    """Generate a same-game-multi price. Body: {"sport", "fixture_id",
    "legs": [{"label", "prob", ...}]}. Uses the connected engine's correlated
    sgm_quote when available, else the independent product (flagged)."""
    from sportsdata_agents.quant.sgm import price_sgm

    legs = list(body.get("legs") or [])
    if len(legs) < 2:
        return JSONResponse({"warning": "a same-game multi needs at least 2 legs"},
                            status_code=400)
    result: dict[str, Any] = price_sgm(
        str(body.get("sport", "")), str(body.get("fixture_id", "")),
        dict(body.get("quotes") or {}), legs)
    return JSONResponse(result)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (STATIC_DIR / "index.html").read_text()


# static assets (app.js / styles.css); mounted after the explicit routes
from fastapi.staticfiles import StaticFiles  # noqa: E402

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
