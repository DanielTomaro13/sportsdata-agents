"""Deterministic platform health — MCP doctor + feed freshness + site status.

No LLM in the path: it runs the same three ops tools the conductor's `ops_health`
job uses and returns a structured result. Factored here so the CLI (`agents ops
health`) and the in-app operator panel trigger render the *same* check, not two
drifting copies.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def run_health(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    """Run doctor + feed-health + site-status and return a structured summary."""
    from sportsdata_agents.tools.ops import ops_tools

    tools = {t.name: t for t in ops_tools(session_factory)}
    doctor = await tools["run_doctor"].execute({})
    feeds = await tools["feed_health"].execute({"hours": 6})
    site = await tools["site_status"].execute({})

    stale = feeds.get("stale_feeds") or []
    ok = bool(doctor.get("ok")) and not stale and bool(site.get("ok"))
    return {
        "ok": ok,
        "doctor": {"ok": bool(doctor.get("ok")), "output": (doctor.get("output") or "")[-2000:]},
        "feeds": {
            "providers_active_6h": len(feeds.get("providers") or []),
            "stale_feeds": stale,
            "disabled_feeds": feeds.get("disabled_feeds") or [],
        },
        "site": {
            "ok": bool(site.get("ok")),
            "latency_ms": site.get("latency_ms"),
            "playback_mode": bool(site.get("playback_mode")),
            "error": site.get("error") or site.get("status_code"),
        },
    }


def summarise_health(h: dict[str, Any]) -> list[str]:
    """One line per facet, for the CLI/log (the panel renders the dict directly)."""
    lines = [f"doctor: {'✓ ok' if h['doctor']['ok'] else '✗ FAILING'}"]
    if not h["doctor"]["ok"]:
        lines.append(h["doctor"]["output"])
    lines.append(f"providers active (6h): {h['feeds']['providers_active_6h']}")
    for stale in h["feeds"]["stale_feeds"]:
        lines.append(f"stale: {stale['feed']} — {stale['reason']}")
    if h["feeds"]["disabled_feeds"]:
        lines.append(f"disabled: {', '.join(h['feeds']['disabled_feeds'])}")
    if not h["feeds"]["stale_feeds"]:
        lines.append("✓ no stale feeds")
    site = h["site"]
    if site["ok"]:
        lines.append(f"✓ site up ({site['latency_ms']}ms{', playback' if site['playback_mode'] else ''})")
    else:
        lines.append(f"✗ site DOWN: {site['error']}")
    return lines
