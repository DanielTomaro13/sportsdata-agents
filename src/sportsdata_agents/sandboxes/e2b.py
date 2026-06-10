"""E2B sandbox backend (M1.3, D5) — true isolation + ENFORCED egress allow-list.

The production backend: per-run cloud microVM, secrets injected per-run, network
restricted to the allow-list. Requires ``E2B_API_KEY`` (and the ``e2b-code-
interpreter`` package, an optional extra) — built test-driven against the documented
API; live verification awaits a key.
"""

from __future__ import annotations

import logging
import os

from sportsdata_agents.sandboxes.base import DEFAULT_TIMEOUT_S, NetworkPolicy, SandboxResult

logger = logging.getLogger(__name__)

EGRESS_ALLOW_LIST = (
    "pypi.org",
    "files.pythonhosted.org",
)


class E2BSandbox:
    def __init__(self) -> None:
        if not os.environ.get("E2B_API_KEY"):
            raise RuntimeError("E2B_API_KEY not set — use LocalSubprocessSandbox locally or configure E2B")

    async def run(
        self,
        code: str,
        *,
        files: dict[str, bytes] | None = None,
        env: dict[str, str] | None = None,
        network_policy: NetworkPolicy = "none",
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> SandboxResult:
        try:
            from e2b_code_interpreter import AsyncSandbox  # optional extra
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("pip install 'sportsdata-agents[sandbox]' for the E2B backend") from e

        allowed = list(EGRESS_ALLOW_LIST) if network_policy == "allow_listed" else []
        sandbox = await AsyncSandbox.create(
            timeout=int(timeout_s),
            envs=env or {},
            # E2B network controls: deny-all unless allow-listed / fully open
            allow_internet_access=network_policy == "open",
            allowed_domains=allowed or None,
        )
        try:
            for name, content in (files or {}).items():
                await sandbox.files.write(name, content)
            execution = await sandbox.run_code(code)
            artifacts: dict[str, bytes] = {}
            for entry in await sandbox.files.list("."):
                if entry.name.endswith((".png", ".csv", ".json", ".html")):
                    artifacts[entry.name] = await sandbox.files.read(entry.name, format="bytes")
            return SandboxResult(
                ok=execution.error is None,
                stdout="".join(execution.logs.stdout)[:64_000],
                stderr=(str(execution.error) if execution.error else "")[:64_000],
                artifacts=artifacts,
            )
        finally:
            await sandbox.kill()
