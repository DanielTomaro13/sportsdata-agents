"""M1.3 — sandbox: local backend isolation, caps, artifacts, run_python gating."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sportsdata_agents.sandboxes import LocalSubprocessSandbox, get_sandbox
from sportsdata_agents.tools.registry import NATIVE_TOOLS

pytestmark = pytest.mark.unit


async def test_runs_code_and_captures_stdout() -> None:
    result = await LocalSubprocessSandbox().run("print(6 * 7)")
    assert result.ok and result.stdout.strip() == "42"


async def test_pandas_analysis_runs() -> None:
    """The M1.3 exit-gate computation: pandas in the sandbox, verified result."""
    code = (
        "import pandas as pd\n"
        "df = pd.DataFrame({'pts': [110, 95, 121, 103]})\n"
        "print(int(df['pts'].mean()))\n"
    )
    result = await LocalSubprocessSandbox().run(code, timeout_s=120)
    assert result.ok, result.stderr
    assert result.stdout.strip() == "107"


async def test_failure_and_timeout_are_reported_not_raised() -> None:
    bad = await LocalSubprocessSandbox().run("raise RuntimeError('boom')")
    assert not bad.ok and "boom" in bad.stderr
    slow = await LocalSubprocessSandbox().run("while True: pass", timeout_s=1.5)
    assert not slow.ok and "timeout" in slow.stderr


async def test_input_files_and_artifacts_round_trip(tmp_path: Path) -> None:
    code = (
        "data = open('in.txt').read()\n"
        "open('out.txt', 'w').write(data.upper())\n"
        "print('done')\n"
    )
    result = await LocalSubprocessSandbox().run(code, files={"in.txt": b"hello"})
    assert result.ok
    assert result.artifacts == {"out.txt": b"HELLO"}


async def test_file_escape_rejected() -> None:
    with pytest.raises(ValueError, match="escapes"):
        await LocalSubprocessSandbox().run("print(1)", files={"../evil.txt": b"x"})


def test_factory_prefers_e2b_when_keyed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    assert isinstance(get_sandbox(), LocalSubprocessSandbox)
    monkeypatch.setenv("E2B_API_KEY", "k")
    from sportsdata_agents.sandboxes.e2b import E2BSandbox

    assert isinstance(get_sandbox(), E2BSandbox)


async def test_run_python_tool_saves_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    out = await NATIVE_TOOLS["run_python"].execute(
        {"code": "open('chart.txt','w').write('fake-chart'); print('saved')"}
    )
    assert out["ok"] and "saved" in out["stdout"]
    assert len(out["artifacts"]) == 1
    assert Path(out["artifacts"][0]).read_text() == "fake-chart"


async def test_run_python_requires_ephemeral_sandbox_spec() -> None:
    from sportsdata_agents.agents.runtime import AgentRuntime
    from sportsdata_agents.agents.spec import AgentSpec

    spec = AgentSpec.model_validate(
        {"id": "x", "display_name": "x", "system_prompt": "x", "tools": {"native": ["run_python"]}}
    )

    class P:
        async def complete(self, *a: Any, **kw: Any) -> Any: ...

    from sportsdata_agents.workspace import Workspace

    with pytest.raises(ValueError, match="ephemeral"):
        async with AgentRuntime(spec, provider=P(), workspace=Workspace()):
            pass
