# Continuous operation

**These are files, not installed jobs.** Nothing here runs until you load it.

## The gap they close

The warehouse is populated by `agents ingest` and settled by `agents results`.
Neither has ever been scheduled. As of 2026-08-05 that showed up as:

- **1,404,943 odds rows spanning nine days** (22–31 July) and nothing since —
  the captures came from manual runs during a working session, and stopped when
  the session did.
- **`event_results` empty**, until a manual run on 05 Aug put 562 rows in it.

Nothing was failing. There was simply no operator process, and no signal that
one was missing — the odds table looked healthy right up until you asked it a
question about a date it did not cover.

The consequence is that every measurement layer built on top — `scoreboard`,
`backtest`, `signal_bench`, the betting shadow account, and calibration — has
never had a real sample. A replay export over 120 days yielded **one** usable
fixture, because the games with odds and the games with results barely overlap.

## Why periodic rather than `--loop`

`agents ingest --loop` runs forever in one process. A long-running loop dies
with a closed laptop, a sleep, or an unhandled exception, and nothing notices —
which is the failure mode that produced the nine-day window above.

`--cron 900` is the stateless equivalent: each invocation runs only the feeds
whose own interval boundary was crossed in the last 900 seconds. launchd then
owns the liveness, restarts are free, and a missed window costs one cycle rather
than every cycle after it.

Results run **hourly, not daily**, because some sources serve only a live/today
board and lose the day once it rolls over. (The NBA source is separately blocked
— HTTP 403 bot-detection on `cdn.nba.com` — and needs its own fix regardless.)

## Install

```bash
mkdir -p ~/Library/Logs/sportsdata-agents
cp ops/launchd/*.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.danieltomaro.sportsdata-ingest.plist
launchctl load ~/Library/LaunchAgents/com.danieltomaro.sportsdata-results.plist
```

Both use `RunAtLoad`, so the first cycle fires immediately.

## Check it is alive

```bash
launchctl print gui/$(id -u)/com.danieltomaro.sportsdata-ingest | grep -E "state|last exit"
tail -20 ~/Library/Logs/sportsdata-agents/ingest.err.log
```

The question worth asking of the warehouse itself is not "does it have rows" but
**"how recent is the newest one"** — that is the check that would have caught
this five days earlier:

```bash
.venv/bin/python -c "
import asyncio; from dotenv import load_dotenv; load_dotenv()
from sportsdata_agents.config import get_settings
from sportsdata_agents.data.db import make_engine, make_sessionmaker
from sportsdata_agents.data.models import Price
from sqlalchemy import select, func
async def m():
    e = make_engine(get_settings().database_url)
    async with make_sessionmaker(e)()  as s:
        print('newest price:', (await s.execute(select(func.max(Price.changed_at)))).scalar())
    await e.dispose()
asyncio.run(m())"
```

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.danieltomaro.sportsdata-{ingest,results}.plist
rm ~/Library/LaunchAgents/com.danieltomaro.sportsdata-{ingest,results}.plist
```

## Not scheduled here

`com.danieltomaro.afl-odds`, `nrl-odds` and `tennis-odds` already exist in
`~/Library/LaunchAgents` and are **unrelated** — they drive
`sports-bots/AFL-23-0/scripts/odds-cron.sh` for the site, not this warehouse.
Their presence is what made the warehouse look scheduled when it was not.
