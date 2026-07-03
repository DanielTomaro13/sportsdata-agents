"""Process-level settings (pydantic-settings).

Resolution: defaults < ``.env`` < environment variables (prefix ``SPORTSDATA_AGENTS_``).
Per-workspace configuration lives in :mod:`sportsdata_agents.workspace`; this is the
single-process / deployment config. Secrets are referenced by name and resolved via
:mod:`sportsdata_agents.secrets` (never stored in specs).
"""

from __future__ import annotations

import json
import shlex
from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _default_database_url() -> str:
    """Desktop default: the durable SQLite warehouse under the OS data dir.
    Imported lazily so the env override path never builds the data dir."""
    from sportsdata_agents.paths import warehouse_url

    return warehouse_url()


def _default_mcp_command() -> list[str]:
    """How to launch the data plane. In a PyInstaller bundle, prefer the
    ``sportsdata-mcp`` binary shipped alongside the app (``sys._MEIPASS``) so the
    desktop app is self-contained and needs nothing on PATH; otherwise resolve
    ``sportsdata-mcp`` from PATH (dev / ``pip install``)."""
    import sys

    if getattr(sys, "frozen", False):
        from pathlib import Path

        bundled = Path(getattr(sys, "_MEIPASS", "")) / "sportsdata-mcp"
        if bundled.exists():
            return [str(bundled)]
    return ["sportsdata-mcp"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SPORTSDATA_AGENTS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── environment ──
    env: str = "dev"
    log_level: str = "INFO"

    # ── data layer ──
    # Desktop default: the durable SQLite warehouse under the OS data dir
    # (never /tmp). Servers override with a Postgres URL via the env var.
    database_url: str = Field(default_factory=_default_database_url)

    # ── default local tenant/workspace (one workspace until SaaS, §12) ──
    default_tenant: str = "local"
    default_workspace: str = "local"

    # ── data plane: how to launch sportsdata-mcp (stdio subprocess). Bundled binary
    #    when frozen, else `sportsdata-mcp` from PATH; env var overrides either. ──
    mcp_command: Annotated[list[str], NoDecode] = Field(default_factory=_default_mcp_command)

    # ── observability (D8) ──
    logfire_token: SecretStr | None = None

    # ── pricing-engine seam (optional; the platform runs fully without one) ──
    # none (default) | local (engines package installed here) | remote (hosted API)
    engine_backend: str = "none"
    engine_api_url: str = ""
    engine_api_key: SecretStr | None = None

    @field_validator("mcp_command", mode="before")
    @classmethod
    def _parse_mcp_command(cls, v: object) -> object:
        """Tolerate every way the env var arrives: JSON list, shell-mangled
        bracket form (sourcing .env strips quotes: [/path]), or a plain command."""
        if not isinstance(v, str):
            return v
        s = v.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                return json.loads(s)
            except ValueError:
                return [part.strip().strip("'\"") for part in s[1:-1].split(",") if part.strip()]
        return shlex.split(s)

    # ── local-dev secret fallback (env is always preferred) ──
    # repr=False so secret values never appear in logs/reprs of Settings (§13).
    secrets: dict[str, str] = Field(default_factory=dict, repr=False)


@lru_cache
def get_settings() -> Settings:
    """Cached process settings. Use this everywhere rather than constructing ``Settings``."""
    return Settings()
