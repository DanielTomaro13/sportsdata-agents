"""distill_transcript — the per-run trace the workbench renders (M4.5)."""

from __future__ import annotations

import pytest

from sportsdata_agents.observability.recorder import (
    TRANSCRIPT_CHARS_PER_MESSAGE,
    distill_transcript,
)

pytestmark = pytest.mark.unit


def test_drops_system_keeps_roles_and_tools() -> None:
    out = distill_transcript([
        {"role": "system", "content": "a huge system prompt"},
        {"role": "user", "content": "compare odds"},
        {"role": "assistant", "content": "let me check", "tool_calls": [{"name": "sportsbet_markets"}]},
        {"role": "tool", "content": "odds: 2.1"},
    ])
    assert [m["role"] for m in out] == ["user", "assistant", "tool"]  # system dropped
    assert out[1]["tools"] == ["sportsbet_markets"]
    assert out[2]["content"] == "odds: 2.1"


def test_handles_openai_style_tool_calls_and_truncates() -> None:
    out = distill_transcript([
        {"role": "assistant", "content": "x" * (TRANSCRIPT_CHARS_PER_MESSAGE + 500),
         "tool_calls": [{"function": {"name": "foo"}}]},
    ])
    assert out[0]["tools"] == ["foo"]
    assert len(out[0]["content"]) == TRANSCRIPT_CHARS_PER_MESSAGE  # capped


def test_empty_and_contentless() -> None:
    assert distill_transcript([]) == []
    out = distill_transcript([{"role": "assistant"}])  # no content key
    assert out == [{"role": "assistant", "content": ""}]
