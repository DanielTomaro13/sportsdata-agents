"""Sandbox interface + local backend (M1.3, D5, §10).

``Sandbox.run(code, files, network_policy)`` executes untrusted-ish Python and
returns stdout/stderr/artifacts. Two backends:

- **LocalSubprocessSandbox** — a separate Python process in a temp dir with CPU/
  memory/time rlimits and output caps. *Caveats (documented, not hidden): this is
  PROCESS isolation only. There is no chroot — the child can READ any file the
  operator can (including ``.env``), and on macOS egress cannot be syscall-blocked
  without root, so ``network_policy`` is advisory: file read + open network is a
  working exfiltration path for hostile code.* Acceptable while the only code author
  is our own model on the operator's own prompts; the moment third-party text flows
  into prompts (P2 ingestion), prompt-injected code is a live threat — use E2B.
- **E2BSandbox** (``e2b.py``) — true isolation + enforced egress allow-list; needs
  ``E2B_API_KEY`` (the factory auto-selects it when keyed).

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


def _posix_limits(memory_mb: int, cpu_s: int) -> None:  # pragma: no cover - runs in the child
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
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
                # is_relative_to, not a string prefix: the workdir must not match
                # a sibling whose name merely starts with it
                if not target.is_relative_to(work.resolve()):
                    raise ValueError(f"file name escapes the sandbox: {name!r}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            before = {str(p.relative_to(work)) for p in work.rglob("*") if p.is_file()}
            script = work / "__main__.py"
            script.write_text(code, encoding="utf-8")

            # CPU rlimit follows the caller's cap as a BACKSTOP, one second above the
            # wall clock so the wall timeout (with its clear message) fires first.
            cpu_s = max(1, int(timeout_s)) + 1
            proc = await asyncio.create_subprocess_exec(
                self._python,
                "-I",  # isolated: no user site-packages leakage beyond the interpreter env
                str(script),
                cwd=workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={"PATH": "/usr/bin:/bin", "HOME": workdir, **(env or {})},
                preexec_fn=(lambda: _posix_limits(self._memory_mb, cpu_s)) if sys.platform != "win32" else None,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return SandboxResult(ok=False, stdout="", stderr=f"timeout after {timeout_s:.0f}s")

            artifacts: dict[str, bytes] = {}
            for path in work.rglob("*"):  # recursive: charts saved into subdirs count too
                if not path.is_file():
                    continue
                rel = str(path.relative_to(work))
                if rel in before or rel == "__main__.py":
                    continue
                if path.stat().st_size <= 5_000_000:
                    artifacts[rel] = path.read_bytes()

            return SandboxResult(
                ok=proc.returncode == 0,
                stdout=stdout.decode(errors="replace")[:MAX_OUTPUT_BYTES],
                stderr=stderr.decode(errors="replace")[:MAX_OUTPUT_BYTES],
                artifacts=artifacts,
            )
