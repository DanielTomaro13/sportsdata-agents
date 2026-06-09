"""Process-level settings (pydantic-settings).

Resolution: defaults < ``.env`` < environment variables (prefix ``SPORTSDATA_AGENTS_``).
Per-workspace configuration lives in :mod:`sportsdata_agents.workspace`; this is the
single-process / deployment config. Secrets are referenced by name and resolved via
:mod:`sportsdata_agents.secrets` (never stored in specs).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    database_url: str = "postgresql+asyncpg://agents:agents@localhost:5432/sportsdata_agents"

    # ── default local tenant/workspace (one workspace until SaaS, §12) ──
    default_tenant: str = "local"
    default_workspace: str = "local"

    # ── data plane: how to launch sportsdata-mcp (stdio subprocess; pinned v0.1.0) ──
    mcp_command: list[str] = Field(default_factory=lambda: ["sportsdata-mcp"])

    # ── observability (D8) ──
    logfire_token: SecretStr | None = None

    # ── local-dev secret fallback (env is always preferred) ──
    secrets: dict[str, str] = Field(default_factory=dict)


@lru_cache
def get_settings() -> Settings:
    """Cached process settings. Use this everywhere rather than constructing ``Settings``."""
    return Settings()
