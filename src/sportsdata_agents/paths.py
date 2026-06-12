"""OS-conventional storage — the desktop app's home on disk (M4.1).

Every persistent thing the app owns resolves through here, so the same code is
a `/tmp`-free desktop install on a user's Mac and a server deployment in CI.
The locations follow each platform's convention:

- **macOS**   ``~/Library/Application Support/sportsdata/``
- **Windows** ``%APPDATA%\\sportsdata\\``
- **Linux**   ``$XDG_DATA_HOME`` or ``~/.local/share/sportsdata/``

``SPORTSDATA_AGENTS_DATA_DIR`` overrides the root (tests, servers, portable
installs). The legacy ``SPORTSDATA_AGENTS_VAR_DIR`` (the old ``~/.sportsdata-agents``)
still resolves the ops subdir so nothing already on disk is orphaned — and
``migrate_legacy_layout`` moves it into the new home once, on first app start.

Nothing here ever lands in ``/tmp``: the warehouse, backups, specs, skills,
logs and ops state are all durable across reboots, which is the whole point of
the desktop move.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "sportsdata"
_LEGACY_DIR = Path.home() / ".sportsdata-agents"


def _platform_root() -> Path:
    override = os.environ.get("SPORTSDATA_AGENTS_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def data_dir() -> Path:
    """The app's root data directory (created on demand)."""
    root = _platform_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _sub(name: str) -> Path:
    path = data_dir() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def warehouse_path() -> Path:
    """The SQLite odds warehouse file (durable — never `/tmp`)."""
    return data_dir() / "warehouse.db"


def warehouse_url() -> str:
    """The default DATABASE_URL for a desktop install."""
    return f"sqlite+aiosqlite:///{warehouse_path()}"


def ops_dir() -> Path:
    """ops_state.json, per-job locks, custodian state. Honours the legacy
    VAR_DIR override so an existing ops state keeps resolving."""
    legacy = os.environ.get("SPORTSDATA_AGENTS_VAR_DIR")
    if legacy:
        path = Path(legacy)
        path.mkdir(parents=True, exist_ok=True)
        return path
    return _sub("ops")


def backups_dir() -> Path:
    return _sub("backups")


def specs_dir() -> Path:
    """User-authored agent specs (the agent-builder writes here)."""
    return _sub("specs")


def skills_dir() -> Path:
    return _sub("skills")


def logs_dir() -> Path:
    return _sub("logs")


def log_path(name: str) -> Path:
    """A named log file under the logs dir — the desktop replacement for the
    scheduler's `/tmp/agents-*.log` defaults."""
    return logs_dir() / name


def desk_dir() -> Path:
    """The user-chosen 'desk' folder agents export reports/CSVs into — the
    Cursor-workspace equivalent. Defaults under the data dir until the wizard
    sets ``SPORTSDATA_AGENTS_DESK_DIR`` to somewhere the user actually opens."""
    override = os.environ.get("SPORTSDATA_AGENTS_DESK_DIR")
    path = Path(override) if override else data_dir() / "desk"
    path.mkdir(parents=True, exist_ok=True)
    return path


def migrate_legacy_layout() -> list[str]:
    """One-time move of the old ``~/.sportsdata-agents`` contents into the new
    home. Idempotent: only copies entries that don't already exist at the
    target, never deletes the source (a manual cleanup the user can do once
    they've confirmed the move). Returns the names migrated."""
    if not _LEGACY_DIR.is_dir() or data_dir() == _LEGACY_DIR:
        return []
    moved: list[str] = []
    root = data_dir()
    ops = ops_dir()
    # the legacy layout kept everything flat under one dir; map it onto the new
    # sub-structure. Target paths are computed RAW (not via the dir-creating
    # helpers) so a target never appears to pre-exist and gets skipped.
    target_for = {
        "ops_state.json": ops / "ops_state.json",
        "locks": ops / "locks",
        "backups": root / "backups",
        "specs": root / "specs",
        "skills": root / "skills",
        "leads.jsonl": root / "leads.jsonl",
    }
    for entry in sorted(_LEGACY_DIR.iterdir()):
        target = target_for.get(entry.name, root / entry.name)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry.is_dir():
            shutil.copytree(entry, target)
        else:
            shutil.copy2(entry, target)
        moved.append(entry.name)
    return moved
