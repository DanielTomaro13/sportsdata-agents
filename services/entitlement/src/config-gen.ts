// Generates the connect-to-your-AI-tool steps for the fulfilment email. The product is a
// downloadable, signed app (no Python / pip) — the customer runs its `setup` once and it
// self-registers into their AI clients. The whole licence is a single env var, so adding
// feeds later never changes any config (just restart).

// The bundled binary's path once the app is in /Applications — what the app's own `setup`
// writes, and the `command` in the manual fallback block.
export const APP_BIN = "/Applications/sportsdata-mcp.app/Contents/MacOS/sportsdata-mcp";

// Default installer download (a GitHub release). Override with env.LICENCE_DOWNLOAD_URL.
export const DEFAULT_DOWNLOAD_URL = "https://github.com/DanielTomaro13/sportsdata-mcp/releases/latest";

// The Manage-feeds page where a customer assigns which feeds fill their slots.
// Override with env.LICENCE_FEEDS_URL.
export const DEFAULT_FEEDS_URL = "https://danieltomaro13.github.io/sportsdata-site/feeds.html";

// The one-time setup command (self-registers into Claude Desktop / Cursor).
export function setupCommand(key: string): string {
  return `"${APP_BIN}" setup --license ${key}`;
}

// The manual config block (for customers who'd rather paste it themselves).
export function mcpConfigBlock(key: string): string {
  return JSON.stringify(
    {
      mcpServers: {
        sportsdata: {
          command: APP_BIN,
          args: ["serve"],
          env: { SPORTSDATA_LICENSE: key },
        },
      },
    },
    null,
    2,
  );
}

// Where the manual block goes, per tool — included in the fulfilment email.
export const CONFIG_TARGETS: { tool: string; path: string }[] = [
  { tool: "Claude Desktop", path: "~/Library/Application Support/Claude/claude_desktop_config.json" },
  { tool: "Cursor", path: "~/.cursor/mcp.json" },
  { tool: "Other MCP clients", path: "your client's MCP servers / mcpServers config" },
];
