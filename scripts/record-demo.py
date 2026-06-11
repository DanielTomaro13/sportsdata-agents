#!/usr/bin/env python3
"""Re-record site/demo-fallback.json from REAL gateway demo runs.

Replays every curated prompt (gateway DEMO_PROMPTS — the ids the static site's
chips use) through run_demo, refreshes the stats snapshot from the live data
plane, and rewrites the fallback file the playback site ships. Existing titles
(with their emoji) are preserved by id; new prompts get their gateway title.

Costs real model spend (~$0.30/prompt budget) and needs a model key + the MCP
command in .env. Run from the repo root:

    .venv/bin/python scripts/record-demo.py            # all prompts
    .venv/bin/python scripts/record-demo.py nba-finals # just one

Review the diff before committing — the recording IS the public demo. Publish
with scripts/deploy-site.sh after merge.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

FALLBACK = Path(__file__).resolve().parent.parent / "site" / "demo-fallback.json"


async def main(only: set[str]) -> int:
    from sportsdata_agents.gateway.demo import DEMO_PROMPTS, demo_stats, run_demo

    existing = json.loads(FALLBACK.read_text(encoding="utf-8")) if FALLBACK.is_file() else {}
    titles = {p["id"]: p["title"] for p in existing.get("prompts", [])}
    runs = dict(existing.get("runs", {}))
    prompt_text = dict(existing.get("prompt_text", {}))

    stats = await demo_stats()
    out_stats = {
        "providers": stats["providers"],
        "groups": stats["groups"],
        "tools": stats["tools"],
        "as_of": dt.date.today().isoformat(),
    }

    failures = 0
    for entry in DEMO_PROMPTS:
        pid = entry["id"]
        if only and pid not in only:
            continue
        print(f"recording {pid} …", flush=True)
        try:
            result = await run_demo(pid)
        except Exception as e:  # one bad run must not lose the others
            print(f"  ✗ {pid}: {type(e).__name__}: {e}")
            failures += 1
            continue
        runs[pid] = {"tool_calls": result["tool_calls"], "answer": result["answer"]}
        prompt_text[pid] = entry["prompt"]
        print(f"  ✓ {pid}: {len(result['tool_calls'])} tool calls, ${result['cost_usd']}")

    payload = {
        "stats": out_stats,
        "prompts": [{"id": p["id"], "title": titles.get(p["id"], p["title"])} for p in DEMO_PROMPTS],
        "prompt_text": {p["id"]: prompt_text.get(p["id"], p["prompt"]) for p in DEMO_PROMPTS},
        "runs": {p["id"]: runs[p["id"]] for p in DEMO_PROMPTS if p["id"] in runs},
    }
    FALLBACK.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {FALLBACK} (stats {out_stats['providers']}/{out_stats['groups']}/{out_stats['tools']})")
    return 1 if failures else 0


if __name__ == "__main__":
    load_dotenv()
    raise SystemExit(asyncio.run(main(set(sys.argv[1:]))))
