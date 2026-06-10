"""Sandboxes (§10, M1.3): isolated code execution for skills + the data-analysis agent."""

from __future__ import annotations

import os

from sportsdata_agents.sandboxes.base import (
    LocalSubprocessSandbox,
    NetworkPolicy,
    Sandbox,
    SandboxResult,
)

__all__ = ["LocalSubprocessSandbox", "NetworkPolicy", "Sandbox", "SandboxResult", "get_sandbox"]


def get_sandbox() -> Sandbox:
    """The configured backend: E2B when E2B_API_KEY is set, else the local subprocess."""
    if os.environ.get("E2B_API_KEY"):
        from sportsdata_agents.sandboxes.e2b import E2BSandbox

        return E2BSandbox()
    return LocalSubprocessSandbox()
