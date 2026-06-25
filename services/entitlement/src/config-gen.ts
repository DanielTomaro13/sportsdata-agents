// Generates the connect-to-your-AI-tool steps for the fulfilment email. The product is a
// downloadable, signed app (no Python / pip) — the customer runs its `setup` once and it
// self-registers into their AI clients. The whole licence is a single env var, so adding
// feeds later never changes any config (just restart).

// The bundled binary's path once the app is in /Applications — what the app's own `setup`
// writes, and the `command` in the manual fallback block.
export const APP_BIN = "/Applications/sportsdata-mcp.app/Contents/MacOS/sportsdata-mcp";

// The entitlement Worker's own public base — used to build the licence-gated download
// link in the fulfilment email. Override with env.ENTITLEMENT_PUBLIC_URL.
export const DEFAULT_ENTITLEMENT_URL = "https://sportsdata-entitlement.sportsdata.workers.dev";

// The licence-gated download link. The product repo is private, so the binary is served
// through the Worker's /download (it checks the licence, then streams the release asset).
// Preferred form: a download-only, expiring token so the raw key never rides in the URL.
export function downloadTokenUrl(base: string, token: string): string {
  return `${base.replace(/\/$/, "")}/download?token=${encodeURIComponent(token)}`;
}

// Legacy form: the raw key in the query. Still honoured by /download for already-sent
// emails; used as the fallback when DOWNLOAD_TOKEN_SECRET isn't configured yet.
export function downloadUrl(base: string, key: string): string {
  return `${base.replace(/\/$/, "")}/download?key=${encodeURIComponent(key)}`;
}

// The Manage-feeds page where a customer assigns which feeds fill their slots.
// Override with env.LICENCE_FEEDS_URL. Live on the custom domain (the old
// danieltomaro13.github.io/sportsdata-site/ URL still redirects here).
export const DEFAULT_FEEDS_URL = "https://sportsdata-ai.com/feeds.html";

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
