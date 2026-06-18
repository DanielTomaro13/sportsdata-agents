// Ed25519 licence signing — same wire format the MCP/agents licensing verifies:
//   token = base64url(payloadJSON) + "." + base64url(signature)
// The MCP bakes the matching PUBLIC key and verifies the signature over the exact
// payload bytes (offline). The private key lives only as a Worker secret (PKCS8).

function b64urlEncode(bytes: Uint8Array): string {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

let cachedKey: CryptoKey | null = null;

async function signingKey(pkcs8B64: string): Promise<CryptoKey> {
  if (cachedKey) return cachedKey;
  cachedKey = await crypto.subtle.importKey(
    "pkcs8",
    b64ToBytes(pkcs8B64),
    { name: "Ed25519" },
    false,
    ["sign"],
  );
  return cachedKey;
}

export async function signLicence(claims: Record<string, unknown>, pkcs8B64: string): Promise<string> {
  const payload = new TextEncoder().encode(JSON.stringify(claims));
  const key = await signingKey(pkcs8B64);
  const sig = new Uint8Array(await crypto.subtle.sign({ name: "Ed25519" }, key, payload));
  return b64urlEncode(payload) + "." + b64urlEncode(sig);
}
