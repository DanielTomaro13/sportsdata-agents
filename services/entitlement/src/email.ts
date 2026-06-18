// Fulfilment email (Phase 5): on first activation, send the customer their licence key
// and a ready-to-paste MCP config via Resend. Inert without RESEND_API_KEY (so Phase-0
// manual fulfilment keeps working until you wire the key).

import { CONFIG_TARGETS, mcpConfigBlock } from "./config-gen";
import type { Env } from "./index";

export interface EmailGrant {
  allAccess: boolean;
  sportSlots: number;
  gamblingSlots: number;
}

function esc(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function planLine(g: EmailGrant): string {
  if (g.allAccess) return "All-access — every sport &amp; gambling feed.";
  const parts: string[] = [];
  if (g.sportSlots) parts.push(`${g.sportSlots} sport feed${g.sportSlots === 1 ? "" : "s"}`);
  if (g.gamblingSlots)
    parts.push(`${g.gamblingSlots} gambling feed${g.gamblingSlots === 1 ? "" : "s"}`);
  return parts.length ? parts.join(" + ") : "your selected feeds";
}

function licenceEmailHtml(key: string, g: EmailGrant): string {
  const block = esc(mcpConfigBlock(key));
  const targets = CONFIG_TARGETS.map(
    (t) => `<li><b>${esc(t.tool)}</b> — <code>${esc(t.path)}</code></li>`,
  ).join("");
  return `<!doctype html><html><body style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1a1a1a;line-height:1.55;max-width:640px;margin:0 auto;padding:24px">
  <h2 style="margin:0 0 4px">Your sportsdata licence</h2>
  <p style="color:#555;margin:0 0 20px">Plan: <b>${planLine(g)}</b></p>

  <p>Your licence key:</p>
  <p style="font:14px ui-monospace,Menlo,monospace;background:#f4f4f5;border:1px solid #e4e4e7;border-radius:8px;padding:12px 14px;word-break:break-all">${esc(key)}</p>

  <h3 style="margin:24px 0 6px">Connect it to your AI tool</h3>
  <p style="margin:0 0 8px">Make sure the MCP is installed (<code>pip install sportsdata-mcp</code>), then add this to your client's MCP config and restart it:</p>
  <pre style="font:13px ui-monospace,Menlo,monospace;background:#0d1117;color:#e6edf3;border-radius:8px;padding:14px;overflow:auto">${block}</pre>
  <p style="margin:8px 0 4px">Where the config lives:</p>
  <ul style="margin:0 0 8px;padding-left:20px">${targets}</ul>

  <p style="color:#555;font-size:14px">Ask your AI tool to <i>"list available sportsdata groups"</i> to confirm it's connected. Adding a feed later never changes this block — your licence key already carries it, so you just restart.</p>

  <hr style="border:none;border-top:1px solid #e4e4e7;margin:24px 0">
  <p style="color:#888;font-size:12px">sportsdata · keep this key private — it grants your feeds. Gambling feeds may be geo-restricted where you are. Questions? Just reply.</p>
</body></html>`;
}

// Returns true if the email was accepted by Resend. Inert (false) without a key/address.
export async function sendLicenceEmail(
  env: Env,
  to: string,
  key: string,
  g: EmailGrant,
): Promise<boolean> {
  if (!env.RESEND_API_KEY || !to) return false;
  const from = env.LICENCE_FROM_EMAIL || "sportsdata <onboarding@resend.dev>";
  const r = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      authorization: `Bearer ${env.RESEND_API_KEY}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({
      from,
      to,
      subject: "Your sportsdata licence + setup",
      html: licenceEmailHtml(key, g),
    }),
  });
  return r.ok;
}
