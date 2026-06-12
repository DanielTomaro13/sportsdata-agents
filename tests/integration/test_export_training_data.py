"""export_training_data (P4): the DB→file bridge for the modelling sandbox.

The sandbox can't reach the warehouse, so this native tool joins captured price
history with settled results and writes a flat CSV into the desk folder for
run_python to read. Outcomes are labeled only when the result is unambiguous.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import EventResult
from sportsdata_agents.data.repository import TenantScope
from sportsdata_agents.operations.ingestion import record_points
from sportsdata_agents.operations.ingestion.normalizers import PricePoint
from sportsdata_agents.tools.quant import quant_tools

pytestmark = pytest.mark.integration

SCOPE = TenantScope("t", "w")
T0 = dt.datetime(2026, 6, 10, 9, 0, tzinfo=dt.UTC)
T1 = T0 + dt.timedelta(minutes=10)


def _pt(selection: str, odds: float) -> PricePoint:
    return PricePoint(provider="sportsbet", book="Sportsbet", sport="afl",
                      event_external_id="E1", event_name="Dogs v Crows",
                      market="h2h", selection=selection, odds=odds)


async def _export_tool(sf: async_sessionmaker[AsyncSession]) -> object:
    tools = {t.name: t for t in quant_tools(sf, SCOPE)}
    return tools["export_training_data"]


async def test_exports_features_and_labels(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SPORTSDATA_AGENTS_DESK_DIR", str(tmp_path))
    # home moves 2.00 → 1.80 (two change-points); away steady at 2.10 (one)
    await record_points(db_sessionmaker, [_pt("home", 2.00), _pt("away", 2.10)], captured_at=T0)
    await record_points(db_sessionmaker, [_pt("home", 1.80), _pt("away", 2.10)], captured_at=T1)
    async with db_sessionmaker() as s:
        s.add(EventResult(provider="sportsbet", sport="afl", event_external_id="E1",
                          winning_selection="home", settled_at=T1))
        await s.commit()

    tool = await _export_tool(db_sessionmaker)
    out = await tool.execute({"event_external_ids": ["E1"], "filename": "afl.csv"})

    assert out["rows"] == 2 and out["events"] == 1 and out["labeled"] == 2
    text = Path(out["path"]).read_text()
    rows = {ln.split(",")[2]: ln for ln in text.splitlines()[1:]}  # keyed by selection col
    # home: open 2.0, close 1.8, 2 points, drift negative, won
    assert ",2.0,1.8," in rows["home"] and rows["home"].endswith(",home,1")
    assert ",home," in rows["home"] and rows["home"].split(",")[4] == "2"  # n_points
    # away: never moved → 1 point, lost
    assert rows["away"].split(",")[4] == "1" and rows["away"].endswith(",home,0")


async def test_ambiguous_result_leaves_outcome_blank(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two providers reporting different winners for the same ext id → no label
    (the export refuses to guess), but the features still export."""
    monkeypatch.setenv("SPORTSDATA_AGENTS_DESK_DIR", str(tmp_path))
    await record_points(db_sessionmaker, [_pt("home", 1.90)], captured_at=T0)
    async with db_sessionmaker() as s:
        s.add(EventResult(provider="sportsbet", sport="afl", event_external_id="E1",
                          winning_selection="home", settled_at=T1))
        s.add(EventResult(provider="tab", sport="afl", event_external_id="E1",
                          winning_selection="away", settled_at=T1))  # disagreement
        await s.commit()

    tool = await _export_tool(db_sessionmaker)
    out = await tool.execute({"event_external_ids": ["E1"]})
    assert out["rows"] == 1 and out["labeled"] == 0
    line = Path(out["path"]).read_text().splitlines()[1]
    assert line.endswith(",,")  # winning_selection blank, outcome blank


async def test_no_prices_raises(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SPORTSDATA_AGENTS_DESK_DIR", str(tmp_path))
    tool = await _export_tool(db_sessionmaker)
    with pytest.raises(ValueError, match="no captured prices"):
        await tool.execute({"event_external_ids": ["NOPE"]})
    with pytest.raises(ValueError, match="non-empty list"):
        await tool.execute({"event_external_ids": []})
