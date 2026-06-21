// Licence-key hashing for D1 at-rest. The customer-facing key stays `sd_live_…` (emailed,
// presented as the bearer); D1 stores only its SHA-256, so a DB read never yields a usable
// key. Lookups accept hash OR raw during/after the migration (a raw row just stops matching
// once it's been hashed), so there's no flag-day.

export async function hashKey(key: string): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(key));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// A licence key is already hashed iff it's 64 lowercase hex chars (sd_live_… never is).
export function looksHashed(id: string): boolean {
  return /^[0-9a-f]{64}$/.test(id);
}
