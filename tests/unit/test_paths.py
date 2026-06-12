"""OS-conventional storage + legacy migration (M4.1 desktop)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from sportsdata_agents import paths

pytestmark = pytest.mark.unit


def test_data_dir_honours_the_override(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATA_DIR", str(tmp_path / "app"))
    assert paths.data_dir() == tmp_path / "app"
    assert paths.data_dir().is_dir()  # created on demand


def test_warehouse_url_is_durable_sqlite(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATA_DIR", str(tmp_path))
    url = paths.warehouse_url()
    assert url.startswith("sqlite+aiosqlite:///") and url.endswith("warehouse.db")
    # lives under the configured data dir — not a hardcoded ephemeral path
    assert str(tmp_path) in url


def test_default_data_dir_is_not_ephemeral(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no override, the warehouse lands in the OS data dir, never /tmp."""
    monkeypatch.delenv("SPORTSDATA_AGENTS_DATA_DIR", raising=False)
    assert not str(paths._platform_root()).startswith("/tmp/")


def test_subdirs_are_under_the_data_root(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SPORTSDATA_AGENTS_VAR_DIR", raising=False)
    for sub in (paths.ops_dir(), paths.backups_dir(), paths.specs_dir(), paths.logs_dir()):
        assert tmp_path in sub.parents or sub.parent == tmp_path
    assert paths.log_path("ingest.log") == tmp_path / "logs" / "ingest.log"


def test_legacy_var_dir_override_still_resolves_ops(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing VAR_DIR keeps pointing ops state at the old place so nothing
    on disk is orphaned mid-migration."""
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATA_DIR", str(tmp_path / "new"))
    monkeypatch.setenv("SPORTSDATA_AGENTS_VAR_DIR", str(tmp_path / "legacy"))
    assert paths.ops_dir() == tmp_path / "legacy"


def test_migrate_legacy_layout_moves_known_entries(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / ".sportsdata-agents"
    (legacy / "locks").mkdir(parents=True)
    (legacy / "specs").mkdir()
    (legacy / "ops_state.json").write_text(json.dumps({"disabled_feeds": ["x"]}))
    (legacy / "specs" / "my_agent.yaml").write_text("agent: {}")
    monkeypatch.setattr(paths, "_LEGACY_DIR", legacy)
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATA_DIR", str(tmp_path / "app"))
    monkeypatch.delenv("SPORTSDATA_AGENTS_VAR_DIR", raising=False)

    moved = paths.migrate_legacy_layout()
    assert set(moved) == {"locks", "specs", "ops_state.json"}
    assert json.loads((paths.ops_dir() / "ops_state.json").read_text())["disabled_feeds"] == ["x"]
    assert (paths.specs_dir() / "my_agent.yaml").exists()
    # the source is never deleted (manual cleanup), and a second run is a no-op
    assert legacy.is_dir()
    assert paths.migrate_legacy_layout() == []


def test_desk_dir_defaults_under_data_then_honours_override(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SPORTSDATA_AGENTS_DESK_DIR", raising=False)
    assert paths.desk_dir() == tmp_path / "desk"
    chosen = tmp_path / "Documents" / "sportsdata-desk"
    monkeypatch.setenv("SPORTSDATA_AGENTS_DESK_DIR", str(chosen))
    assert paths.desk_dir() == chosen and chosen.is_dir()
