// Generates the connect-to-your-AI-tool config a customer pastes in. The whole licence
// is a single env var — adding feeds later never changes this block (just restart).

// The MCP client config (Claude Desktop / Cursor / any mcpServers-shaped client).
export function mcpConfigBlock(key: string): string {
  return JSON.stringify(
    {
      mcpServers: {
        sportsdata: {
          command: "sportsdata-mcp",
          args: ["serve"],
          env: { SPORTSDATA_LICENSE: key },
        },
      },
    },
    null,
    2,
  );
}

// Where the block goes, per tool — included in the fulfilment email.
export const CONFIG_TARGETS: { tool: string; path: string }[] = [
  { tool: "Claude Desktop", path: "~/Library/Application Support/Claude/claude_desktop_config.json" },
  { tool: "Cursor", path: "~/.cursor/mcp.json" },
  { tool: "Other MCP clients", path: "your client's MCP servers / mcpServers config" },
];
