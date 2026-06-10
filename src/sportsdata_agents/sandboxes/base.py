"""Sandbox interface + local backend (M1.3, D5, §10).

``Sandbox.run(code, files, network_policy)`` executes untrusted-ish Python and
returns stdout/stderr/artifacts. Two backends:

- **LocalSubprocessSandbox** — a separate Python process in a temp dir with CPU/
  memory/time rlimits and output caps. *Caveat (documented, not hidden): on macOS we
  cannot syscall-block egress without root, so ``network_policy`` is advisory
  locally.* Fine for the local-first phase where the operator trusts their own box.
- **E2BSandbox** (``e2b.py``) — true isolation + enforced egress allow-list; needs
  ``E2B_API_KEY`` and is the production backend (P4).

Secrets are injected per-run via ``env`` and never persisted.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

logger = logging.getLogger(__name__)

NetworkPolicy = Literal["none", "allow_listed", "open"]

MAX_OUTPUT_BYTES = 64_000
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_MEMORY_MB = 1024


@dataclass(frozen=True)
class SandboxResult:
    ok: bool
    stdout: str
    stderr: str
    # files the code wrote into its working dir (name → bytes), e.g. charts
    artifacts: dict[str, bytes] = field(default_factory=dict)


class Sandbox(Protocol):
    async def run(
        self,
        code: str,
        *,
        files: dict[str, bytes] | None = None,
        env: dict[str, str] | None = None,
        network_policy: NetworkPolicy = "none",
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> SandboxResult: ...


def _posix_limits(memory_mb: int) -> None:  # pragma: no cover - runs in the child
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (int(DEFAULT_TIMEOUT_S), int(DEFAULT_TIMEOUT_S)))
    try:
        cap = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    except (ValueError, OSError):
        pass  # RLIMIT_AS unsupported on some macOS versions


class LocalSubprocessSandbox:
    """Process-isolated local runner: temp dir, rlimits, time cap, output caps."""

    def __init__(self, *, python: str | None = None, memory_mb: int = DEFAULT_MEMORY_MB) -> None:
        self._python = python or sys.executable
        self._memory_mb = memory_mb

    async def run(
        self,
        code: str,
        *,
        files: dict[str, bytes] | None = None,
        env: dict[str, str] | None = None,
        network_policy: NetworkPolicy = "none",
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> SandboxResult:
        if network_policy != "none":
            logger.warning("local sandbox cannot ENFORCE network_policy=%s (advisory only)", network_policy)
        with tempfile.TemporaryDirectory(prefix="agents-sbx-") as workdir:
            work = Path(workdir)
            for name, content in (files or {}).items():
                target = (work / name).resolve()
                if not str(target).startswith(str(work.resolve())):
                    raise ValueError(f"file name escapes the sandbox: {name!r}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            before = {p.name for p in work.iterdir()}
            script = work / "__main__.py"
            script.write_text(code, encoding="utf-8")

            proc = await asyncio.create_subprocess_exec(
                self._python,
                "-I",  # isolated: no user site-packages leakage beyond the interpreter env
                str(script),
                cwd=workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={"PATH": "/usr/bin:/bin", "HOME": workdir, **(env or {})},
                preexec_fn=(lambda: _posix_limits(self._memory_mb)) if sys.platform != "win32" else None,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return SandboxResult(ok=False, stdout="", stderr=f"timeout after {timeout_s:.0f}s")

            artifacts: dict[str, bytes] = {}
            for path in work.iterdir():
                if path.name in before or path.name == "__main__.py":
                    continue
                if path.is_file() and path.stat().st_size <= 5_000_000:
                    artifacts[path.name] = path.read_bytes()

            return SandboxResult(
                ok=proc.returncode == 0,
                stdout=stdout.decode(errors="replace")[:MAX_OUTPUT_BYTES],
                stderr=stderr.decode(errors="replace")[:MAX_OUTPUT_BYTES],
                artifacts=artifacts,
            )
