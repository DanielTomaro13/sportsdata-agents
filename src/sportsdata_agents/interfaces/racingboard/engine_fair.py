"""The sportsdata racing engine as a fair-price source for the board.

The board's own fair comes from de-vigged Betfair/tote (market opinion). This
adds the FORM engine's opinion: ``engine-form:racing`` win probabilities from
the sportsdata-agents warehouse, so a runner's FAIR/VAL can reflect the model,
not just the crowd.

Bridge: the board keys a race ``{code}:{venue_mnem}:{race_no}:{date}``; the
warehouse keys engine-form predictions by the TAB race key
``{date}:{raceType}:{venueMnemonic}:{raceNumber}`` (RaceForm.race_key). Same
four fields, reordered — an exact, deterministic join, no fuzzy matching.

Everything here degrades to ``{}`` when there's no warehouse / no engine
predictions (the standalone or replay board), so the board keeps working with
just Betfair + tote.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# built once, lazily; None once we know the warehouse is unreachable so we stop
# retrying every poll
_sessionmaker: Any = None
_disabled = False


def _agents_key(date: str, code: str, venue_mnem: str, race_no: int) -> str:
    return f"{date}:{code}:{venue_mnem}:{race_no}"


async def _get_sessionmaker() -> Any:
    global _sessionmaker, _disabled
    if _disabled:
        return None
    if _sessionmaker is not None:
        return _sessionmaker
    try:
        from sportsdata_agents.config import get_settings
        from sportsdata_agents.data.db import make_engine, make_sessionmaker

        url = get_settings().database_url
        engine = make_engine(url)
        _sessionmaker = make_sessionmaker(engine)
        return _sessionmaker
    except Exception as exc:  # no warehouse configured / import unavailable
        logger.info("racing engine fair source disabled: %s", exc)
        _disabled = True
        return None


async def engine_prices(
    *, date: str, code: str, venue_mnem: str, race_no: int,
    session_factory: Any = None,
) -> dict[int, float]:
    """{saddle_number: engine win probability} for a race, or {} when the
    engine has nothing for it (or no warehouse is configured).

    ``session_factory`` overrides the cached warehouse sessionmaker (tests)."""
    global _disabled
    sf = session_factory or await _get_sessionmaker()
    if sf is None:
        return {}
    try:
        from sqlalchemy import select

        from sportsdata_agents.data.models import ModelArtifact, Prediction

        key = _agents_key(date, code, venue_mnem, race_no)
        async with sf() as session:
            rows = (await session.execute(
                select(Prediction.selection, Prediction.prob)
                .join(ModelArtifact, ModelArtifact.id == Prediction.model_id)
                .where(ModelArtifact.name == "engine-form:racing",
                       Prediction.market == "win",
                       Prediction.event_external_id == key)
                .order_by(Prediction.predicted_at.desc())
            )).all()
        out: dict[int, float] = {}
        for selection, prob in rows:  # newest first — older passes never overwrite
            sel = str(selection)
            if sel.isdigit():
                out.setdefault(int(sel), float(prob))
        return out
    except Exception as exc:  # a bad query must never sink a poll
        logger.warning("engine fair lookup failed for %s R%s: %s",
                       venue_mnem, race_no, exc)
        # only the shared cached path disables (a warehouse with no predictions
        # table / unreachable) — an injected test session never trips it, so it
        # doesn't spam a warning every poll for a board with no engine data
        if session_factory is None:
            _disabled = True
        return {}
