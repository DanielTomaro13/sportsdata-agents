# Hosted MCP channel (D23) — BYO-LLM quickstart

The data plane (`sportsdata-mcp`) speaks standard MCP over stdio, so any MCP
client — Claude Desktop, Claude Code, ChatGPT desktop, Cursor — can use it
directly with the user's OWN model: the low-friction entry that upsells to the
full agent platform.

## Local (stdio — works today)

```json
{
  "mcpServers": {
    "sportsdata": {
      "command": "/path/to/sportsdata-mcp/.venv/bin/sportsdata-mcp",
      "env": { "SPORTSDATA_MCP_GROUPS": "sportsbet.sports,tab.sports,nba.public.cdn" }
    }
  }
}
```

Scope with `SPORTSDATA_MCP_GROUPS` (least privilege — same mechanism the agent
plane uses). Keys for premium providers go in the same `env` block.

## Remote (hosted)

For a hosted channel, front the stdio server with an MCP-over-HTTP bridge
(e.g. `mcp-remote`/supergateway) per tenant, with:
- a per-tenant API key mapped to an allowed `SPORTSDATA_MCP_GROUPS` set,
- rate limits at the proxy,
- no proprietary skills on this surface (D29 — skills stay in the agent plane).

Productising this (auth, metering, tenant keys) is the P4 billing work; the
protocol surface needs no changes.
