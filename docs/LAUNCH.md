# Launch kit — free & open-source announcement

Copy-paste drafts for the channels that matter. Post from your own accounts;
tweak voice as you like. Ordered by expected impact.

---

## 1. Reddit — r/ClaudeAI (also fits r/mcp)

**Title:** I open-sourced my sports-data MCP — ~500 tools across 28 providers (odds, stats, racing, fantasy), free, works with Claude Desktop/Cursor out of the box

**Body:**

I've spent the last few months building a sports-data MCP server and today I
made the whole thing free and open source (MIT).

What it gives your Claude/Cursor:
- **Cross-book betting odds** — Sportsbet, TAB, Betfair, Pinnacle, PointsBet, Unibet, Ladbrokes/Neds, BetR, Dabble, FanDuel
- **Prediction markets** — Kalshi + Polymarket priced like any book
- **Official league feeds** — AFL, NRL, NBA, MLB, Premier League, La Liga, Serie A, cricket, F1 telemetry (OpenF1), WTA
- **Racing** — cards, form, results across the AU books

Zero config: download, run `sportsdata-mcp setup`, and the full catalogue is in
your client. No account, no key (a couple of providers like DataGolf want your
own key). Ask things like "compare tonight's NBA odds across every book" or
"any arbitrage right now?" and it pulls live data.

There's also a full open-source agent workbench (agent team, arb/value
monitors, backtesting with CLV) in a sibling repo if you want more than raw
tools.

Repo: https://github.com/DanielTomaro13/sportsdata-mcp
Site + live demo: https://sportsdata-ai.com

Advisory only — it informs, it never bets. Feedback and provider requests very
welcome; adding a provider is literally one YAML file.

---

## 2. Show HN

**Title:** Show HN: Sportsdata-MCP – 28 sports/odds providers as ~500 MCP tools (MIT)

**Comment (first):**

Author here. This started as a paid product; I open-sourced the lot today.

The interesting engineering bits: every provider is a declarative YAML spec (a
loader turns routes into MCP tools, so adding a bookmaker needs no code); a
capability-tag system makes tools interchangeable across providers ("who prices
this game?" is one discovery call); persisted-GraphQL books get a hash-refresh
tool; and endpoint drift is caught by a nightly contract check that probes one
representative route per provider and alerts only on new failures.

Happy to answer anything about scraping unofficial book APIs politely, MCP
server design, or the economics of open-sourcing it.

---

## 3. X / Twitter thread

1/ I just open-sourced my entire sports-data platform. ~500 MCP tools across
28 providers — live odds from 10+ bookmakers, official league stats, racing,
prediction markets — free, MIT, in your Claude/Cursor in one command. 🧵

2/ "Compare tonight's odds across every book." "Any arbs right now?" "Show me
the Premier League table + top scorers." All grounded in live tool calls, all
in the AI client you already use. github.com/DanielTomaro13/sportsdata-mcp

3/ There's also a full agent workbench — an agent team over the same data with
arbitrage/value monitors, an odds warehouse, CLV-honest backtesting, and
reasoning traces on every reply. Also free, also MIT.
github.com/DanielTomaro13/sportsdata-agents

4/ Why free? Distribution beats margin at zero customers. If it's useful, star
it, break it, send provider requests — a new provider is one YAML file.
sportsdata-ai.com

---

## 4. MCP Discord / community blurb (short)

Just open-sourced **sportsdata-mcp** (MIT): ~500 tools / 28 providers — live
cross-book betting odds (AU books + Pinnacle + Betfair), official league stats
(AFL/NRL/NBA/MLB/EPL/…), racing, Kalshi/Polymarket. Zero-config `setup` writes
your Claude Desktop/Cursor config. Windows + macOS builds or `pip install -e .`.
https://github.com/DanielTomaro13/sportsdata-mcp — provider requests welcome
(one YAML file each).

---

## 5. Registry / directory checklist

- [ ] **Glama** — it already indexed the repo; log in with GitHub and claim the
      listing (glama.ai/mcp/servers), refresh metadata now it's public.
- [ ] **awesome-mcp-servers PR** — adds us under Sports (I can open this).
- [ ] **registry.modelcontextprotocol.io** — the official registry; publish via
      `mcp-publisher` with a server.json (GitHub-auth'd, ~15 min).
- [ ] **mcp.so / other directories** — submission forms, ~5 min each.

## Notes

- Lead with the MCP everywhere; the workbench is the "and there's more" link.
- The AU-book coverage is the differentiator — no other MCP has Sportsbet/TAB/Dabble.
- Always include the advisory-only line for betting-adjacent audiences.
