"""Workbench B4 — chat response trace: the reply carries its run_id so the UI can fetch
the turn's transcript at /runs/{run_id}. (The transcript itself is the #122 capture.)"""

from __future__ import annotations

import uuid

import pytest

from sportsdata_agents.agents.harness import RunResult
from sportsdata_agents.gateway.app import _to_message_out

pytestmark = pytest.mark.unit


def test_message_out_carries_run_id():
    rid = uuid.uuid4()
    rr = RunResult(output="hi", stop_reason="stop", steps=1, tool_call_count=2, cost_usd=0.01, run_id=rid)
    assert _to_message_out(rr).run_id == str(rid)


def test_message_out_run_id_optional():
    rr = RunResult(output="hi", stop_reason="stop", steps=0, tool_call_count=0, cost_usd=0.0)
    assert _to_message_out(rr).run_id is None
