"""Sandboxes (§10, M1.3): isolated code execution for skills + the data-analysis agent."""

from __future__ import annotations

import logging
import os

from sportsdata_agents.sandboxes.base import (
    LocalSubprocessSandbox,
    NetworkPolicy,
    Sandbox,
    SandboxResult,
)

__all__ = ["LocalSubprocessSandbox", "NetworkPolicy", "Sandbox", "SandboxResult", "get_sandbox"]

logger = logging.getLogger(__name__)
_warned_local = False


def get_sandbox() -> Sandbox:
    """The configured backend: E2B when E2B_API_KEY is set, else the local subprocess.

    ``SPORTSDATA_AGENTS_REQUIRE_SANDBOX_ISOLATION=1`` refuses the local fallback —
    set it the moment third-party text flows into prompts (P2 ingestion is live):
    the local backend cannot contain prompt-injected exfiltration (see base.py).
    """
    global _warned_local
    if os.environ.get("E2B_API_KEY"):
        from sportsdata_agents.sandboxes.e2b import E2BSandbox

        return E2BSandbox()
    if os.environ.get("SPORTSDATA_AGENTS_REQUIRE_SANDBOX_ISOLATION", "").lower() in ("1", "true", "yes"):
        raise RuntimeError(
            "sandbox isolation is required (SPORTSDATA_AGENTS_REQUIRE_SANDBOX_ISOLATION) "
            "but E2B_API_KEY is not configured — the local subprocess sandbox cannot "
            "contain hostile code (§10)"
        )
    if not _warned_local:
        _warned_local = True
        logger.warning(
            "using the LOCAL subprocess sandbox: process isolation only — egress is "
            "advisory and the filesystem is readable (fine for self-generated code; "
            "set E2B_API_KEY before untrusted text reaches prompts)"
        )
    return LocalSubprocessSandbox()
