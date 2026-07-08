"""Runtime configuration for the racing money-flow tool.

Everything is overridable via environment variables so the same code runs on a
laptop or a box. The one path that matters is SPORTSDATA_MCP_SRC — the `src`
directory of your local sportsdata-mcp checkout, whose vetted HTTP engine we
import as a library to reach TAB (Akamai-gated) and the corporate books.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_mcp_src() -> str:
    # Sensible default for this machine; override with SPORTSDATA_MCP_SRC.
    guess = Path.home() / "Documents" / "Projects" / "sportsdata-mcp" / "src"
    return os.environ.get("SPORTSDATA_MCP_SRC", str(guess))


@dataclass
class Settings:
    # --- sportsdata-mcp engine (TAB + corporate data layer) ---
    sportsdata_mcp_src: str = field(default_factory=_default_mcp_src)

    # --- polling cadence (seconds) ---
    # Board (all upcoming races) is discovered less often than prices are polled.
    discovery_interval: float = float(os.environ.get("MF_DISCOVERY_INTERVAL", "60"))
    price_interval: float = float(os.environ.get("MF_PRICE_INTERVAL", "8"))
    # Corporate books rate-limit, so price them on a slower cadence than the tote.
    corp_interval: float = float(os.environ.get("MF_CORP_INTERVAL", "20"))

    # How far ahead to track races for the board (minutes to jump).
    horizon_minutes: int = int(os.environ.get("MF_HORIZON_MINUTES", "45"))
    # Max races polled at full cadence at once (protects the upstreams).
    max_active_races: int = int(os.environ.get("MF_MAX_ACTIVE_RACES", "12"))

    # TAB jurisdiction for the meetings spine.
    jurisdiction: str = os.environ.get("MF_JURISDICTION", "NSW")

    # Racing codes to track: R=thoroughbred, G=greyhound, H=harness.
    codes: tuple[str, ...] = tuple(os.environ.get("MF_CODES", "R,G,H").split(","))

    # --- source toggles ---
    enable_betfair: bool = os.environ.get("MF_BETFAIR", "1") == "1"
    enable_tab: bool = os.environ.get("MF_TAB", "1") == "1"
    enable_corporate: bool = os.environ.get("MF_CORPORATE", "1") == "1"

    # Time-series retention per race (number of snapshots kept in memory).
    history_len: int = int(os.environ.get("MF_HISTORY_LEN", "300"))

    # HTTP server.
    host: str = os.environ.get("MF_HOST", "127.0.0.1")
    # Honour a harness-assigned PORT (preview/hosting) before MF_PORT/default.
    port: int = int(os.environ.get("PORT") or os.environ.get("MF_PORT") or "8000")


settings = Settings()

CODE_LABEL = {"R": "Thoroughbred", "G": "Greyhound", "H": "Harness"}
