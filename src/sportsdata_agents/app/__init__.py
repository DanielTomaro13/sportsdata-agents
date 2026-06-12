"""The desktop app process (M4.1).

``agents app`` is the single supervised process a desktop install runs — no
crontab, no launchd, no `.env` required:

- the **gateway** (uvicorn, localhost-only) so the chat UI / Slack / Discord
  can reach the team;
- the **conductor loop** in-process — ``run_tick`` every 60s — so ingest,
  resolve, monitor and the custodian all happen as the app's heartbeat;
- **MCP subprocess supervision** is handled inside each job (the existing
  manager already restarts its subprocess), so the supervisor only owns the
  two long-lived coroutines.

First start migrates the legacy `~/.sportsdata-agents` layout into the OS data
dir, then runs forever until SIGINT/SIGTERM. The same job registry powers the
server `agents schedule --cron 60` cron mode — two drivers, one engine.
"""

from .supervisor import run_app

__all__ = ["run_app"]
