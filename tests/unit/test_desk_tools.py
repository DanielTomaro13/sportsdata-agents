"""Desk-folder export tools (P4 §4.2): write into the desk folder, never outside it."""

from __future__ import annotations

from pathlib import Path

import pytest

from sportsdata_agents import paths
from sportsdata_agents.tools.desk import export_csv, write_report

pytestmark = pytest.mark.unit


@pytest.fixture()
def desk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("SPORTSDATA_AGENTS_DESK_DIR", str(tmp_path))
    return tmp_path


async def test_export_csv_writes_table(desk: Path) -> None:
    rows = [{"book": "TAB", "odds": 2.10}, {"book": "Sportsbet", "odds": 2.05}]
    out = await export_csv({"filename": "board", "rows": rows})
    assert out["rows"] == 2 and out["columns"] == ["book", "odds"]
    text = (desk / "board.csv").read_text()  # .csv appended
    assert text.splitlines()[0] == "book,odds"
    assert "TAB,2.1" in text and "Sportsbet,2.05" in text


async def test_export_csv_union_columns_and_blanks(desk: Path) -> None:
    rows = [{"a": 1}, {"a": 2, "b": 3}]  # b only on the second row
    out = await export_csv({"filename": "mixed.csv", "rows": rows})
    assert out["columns"] == ["a", "b"]
    lines = (desk / "mixed.csv").read_text().splitlines()
    assert lines == ["a,b", "1,", "2,3"]  # missing b is blank, not "None"


async def test_export_csv_explicit_columns_subset(desk: Path) -> None:
    rows = [{"x": 1, "secret": "drop", "y": 2}]
    out = await export_csv({"filename": "f.csv", "rows": rows, "columns": ["x", "y"]})
    assert out["columns"] == ["x", "y"]
    assert "secret" not in (desk / "f.csv").read_text()


async def test_write_report_appends_md(desk: Path) -> None:
    out = await write_report({"filename": "brief", "content": "# Hello\n\nbody"})
    assert out["path"].endswith("brief.md")
    assert (desk / "brief.md").read_text() == "# Hello\n\nbody"


async def test_write_report_keeps_explicit_extension(desk: Path) -> None:
    await write_report({"filename": "notes.txt", "content": "x"})
    assert (desk / "notes.txt").is_file()


async def test_empty_inputs_rejected(desk: Path) -> None:
    with pytest.raises(ValueError, match="rows must be"):
        await export_csv({"filename": "f.csv", "rows": []})
    with pytest.raises(ValueError, match="content is required"):
        await write_report({"filename": "f.md", "content": ""})
    with pytest.raises(ValueError, match="filename is required"):
        await export_csv({"filename": "", "rows": [{"a": 1}]})


@pytest.mark.parametrize("evil", ["../escape.csv", "/etc/passwd", "../../x.csv", "sub/../../../x.csv"])
async def test_traversal_is_rejected(desk: Path, evil: str) -> None:
    with pytest.raises(ValueError, match="escapes the desk folder"):
        await export_csv({"filename": evil, "rows": [{"a": 1}]})
    # nothing was written outside the desk folder
    assert not Path("/etc/passwd.csv").exists()


async def test_subfolders_allowed(desk: Path) -> None:
    out = await export_csv({"filename": "boards/afl.csv", "rows": [{"a": 1}]})
    assert (desk / "boards" / "afl.csv").is_file()
    assert out["path"] == str(desk / "boards" / "afl.csv")


def test_set_desk_dir_persists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # env var must NOT be set, or it would win over the persisted choice
    monkeypatch.delenv("SPORTSDATA_AGENTS_DESK_DIR", raising=False)
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATA_DIR", str(tmp_path / "data"))
    target = tmp_path / "my-desk"
    paths.set_desk_dir(target)
    assert paths.desk_dir().resolve() == target.resolve()
