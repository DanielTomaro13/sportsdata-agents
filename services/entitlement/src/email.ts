// Fulfilment email (Phase 5): on first activation, send the customer their licence key
// and a ready-to-paste MCP config via Resend. Inert without RESEND_API_KEY (so Phase-0
// manual fulfilment keeps working until you wire the key).

import {
  CONFIG_TARGETS,
  DEFAULT_DOWNLOAD_URL,
  DEFAULT_FEEDS_URL,
  mcpConfigBlock,
  setupCommand,
} from "./config-gen";
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

function licenceEmailHtml(key: string, g: EmailGrant, downloadUrl: string, feedsUrl: string): string {
  const block = esc(mcpConfigBlock(key));
  const setupCmd = esc(setupCommand(key));
  const targets = CONFIG_TARGETS.map(
    (t) => `<li><b>${esc(t.tool)}</b> — <code>${esc(t.path)}</code></li>`,
  ).join("");
  // All-access grants every feed, so there's nothing to choose. Otherwise the customer
  // MUST pick which feeds fill their slots, or the app serves nothing — so that's step 1.
  const choose = !g.allAccess;
  const n = choose ? g.sportSlots + g.gamblingSlots : 0;
  const s1 = choose ? "1" : "", dl = choose ? "2" : "1", su = choose ? "3" : "2";
  const chooseStep = choose
    ? `<p style="margin:0 0 10px"><b>${s1}.</b> <a href="${esc(feedsUrl)}" style="color:#2563eb">Choose your feeds</a> — open that page, paste your licence key (above), pick your ${n} feed${n === 1 ? "" : "s"}, and Save. <b>Do this first</b> — until you choose, the app serves nothing.</p>`
    : "";
  return `<!doctype html><html><body style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1a1a1a;line-height:1.55;max-width:640px;margin:0 auto;padding:24px">
  <h2 style="margin:0 0 4px">Your sportsdata licence</h2>
  <p style="color:#555;margin:0 0 20px">Plan: <b>${planLine(g)}</b></p>

  <p>Your licence key:</p>
  <p style="font:14px ui-monospace,Menlo,monospace;background:#f4f4f5;border:1px solid #e4e4e7;border-radius:8px;padding:12px 14px;word-break:break-all">${esc(key)}</p>

  <h3 style="margin:24px 0 6px">Set it up — ${choose ? "three" : "two"} steps</h3>
  ${chooseStep}
  <p style="margin:0 0 10px"><b>${dl}.</b> <a href="${esc(downloadUrl)}" style="color:#2563eb">Download the sportsdata-mcp app</a> and drag it into Applications. No Python needed — it bundles everything.<br>
  <span style="color:#888;font-size:13px">First launch on macOS: this early build isn't notarized yet, so <b>right-click the app → Open</b> (don't double-click), then click <b>Open</b> in the dialog. One time only.</span></p>
  <p style="margin:0 0 6px"><b>${su}.</b> Run this once in Terminal — it registers itself with your AI clients using your licence:</p>
  <pre style="font:13px ui-monospace,Menlo,monospace;background:#0d1117;color:#e6edf3;border-radius:8px;padding:14px;overflow:auto">${setupCmd}</pre>
  <p style="color:#555;font-size:14px;margin:8px 0 0">Then restart your AI client and ask it to <i>"list available sportsdata groups"</i>. Changing feeds later just means re-saving on that page + a restart — your licence already carries the list, so no re-download.</p>

  <details style="margin:18px 0 0">
    <summary style="color:#555;font-size:13px;cursor:pointer">Prefer to paste the config yourself?</summary>
    <pre style="font:13px ui-monospace,Menlo,monospace;background:#0d1117;color:#e6edf3;border-radius:8px;padding:14px;overflow:auto;margin-top:10px">${block}</pre>
    <p style="margin:6px 0 0;font-size:13px;color:#555">Where it lives:</p>
    <ul style="margin:4px 0 0;padding-left:20px;font-size:13px;color:#555">${targets}</ul>
  </details>

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
  try {
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
        html: licenceEmailHtml(
          key,
          g,
          env.LICENCE_DOWNLOAD_URL || DEFAULT_DOWNLOAD_URL,
          env.LICENCE_FEEDS_URL || DEFAULT_FEEDS_URL,
        ),
      }),
    });
    if (!r.ok) console.error(`resend rejected fulfilment email to ${to}: ${r.status}`);
    return r.ok;
  } catch (e) {
    // A network throw (DNS / timeout) must not propagate — the caller releases the
    // one-time email claim on a false return so a later webhook event retries.
    console.error(`resend send threw for ${to}:`, e);
    return false;
  }
}
