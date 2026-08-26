"""FastAPI app: the sports board over the warehouse (sharp line + book value +
engine SGM price generator)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .live import live_enabled, run_poller
from .warehouse import game_detail, list_games, list_specials

STATIC_DIR = Path(__file__).parent / "static"


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """When SPORTSBOARD_LIVE is set, poll live upstreams in-process (self-contained
    live board). Default off — the server stays a pure warehouse reader."""
    task = asyncio.create_task(run_poller()) if live_enabled() else None
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Sports Board", lifespan=_lifespan)
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


@app.get("/api/specials")
async def api_specials(days: float = 90.0) -> JSONResponse:
    """Novelty/outright markets — everything the two-sided games gate drops."""
    async with _sessionmaker()() as s:
        return JSONResponse({"specials": await list_specials(s, days=days)})


@app.get("/api/game/{fixture_id}")
async def api_game(fixture_id: str) -> JSONResponse:
    async with _sessionmaker()() as s:
        detail = await game_detail(s, fixture_id)
    if detail is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(detail)


@app.get("/api/sgm/books")
async def api_sgm_books(fixture_id: str) -> JSONResponse:
    """Which bookmakers can quote an SGM for this fixture (and why not, else)."""
    from sportsdata_agents.interfaces.sportsboard import live, sgm_books

    async with _sessionmaker()() as session:
        books = await sgm_books.available_books(session, fixture_id, live.current_manager)
    return JSONResponse({"books": books})


@app.post("/api/sgm")
async def api_sgm(body: dict) -> JSONResponse:
    """Generate a same-game-multi price. Body: {"sport", "fixture_id",
    "legs": [{"label", "prob", ...}], "bookmaker": optional}.

    With a bookmaker: the BOOK prices the combination via sportsdata-mcp —
    a real, bookable quote with the book's own correlation adjustment.
    Without one: the connected engine's correlated sgm_quote when available,
    else the independent product (flagged)."""
    from sportsdata_agents.quant.sgm import price_sgm

    legs = list(body.get("legs") or [])
    if len(legs) < 2:
        return JSONResponse({"warning": "a same-game multi needs at least 2 legs"},
                            status_code=400)

    bookmaker = str(body.get("bookmaker") or "").strip().lower()
    if bookmaker:
        from sportsdata_agents.interfaces.sportsboard import live, sgm_books

        async with _sessionmaker()() as session:
            quoted: dict[str, Any] = await sgm_books.quote(
                session, live.current_manager, bookmaker,
                str(body.get("fixture_id", "")), legs)
        return JSONResponse(quoted)

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
