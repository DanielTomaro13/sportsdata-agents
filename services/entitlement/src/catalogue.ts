// Mapping Stripe SKUs → feed-slot grants, and which feeds run on our credentials.

export type Sku = "base" | "sport_addon" | "gambling_addon" | "all_access";

export interface Grant {
  sport_slots: number;
  gambling_slots: number;
  all_access: boolean;
}

// A subscription's line items → the slots they grant. SKUs come from each price's
// product metadata `sportsdata_sku` (set by scripts/setup-stripe.py).
export function grantFromItems(items: { sku: string; quantity: number }[]): Grant {
  let sport = 0, gambling = 0, all = false;
  for (const it of items) {
    switch (it.sku) {
      case "base": sport += 5; break;            // 5 sport feeds included
      case "sport_addon": sport += it.quantity; break;
      case "gambling_addon": gambling += it.quantity; break;
      case "all_access": all = true; break;
    }
  }
  return { sport_slots: sport, gambling_slots: gambling, all_access: all };
}

// Feeds that run on OUR upstream credentials → served via the proxy, never shipped
// inside a self-host build (DataGolf = our paid key; TAB = our OAuth client).
export const PROXIED_PROVIDERS = new Set(["datagolf", "tab"]);

// Provider → slot kind (mirrors the site catalogue's sport/gambling split). Drives
// per-kind slot enforcement when a customer assigns feeds. Keep in sync with the
// catalogue; an unlisted provider is rejected by the assignment endpoint.
export const PROVIDER_KIND: Record<string, "sport" | "gambling"> = {
  // sport & stats
  afl: "sport", cricketaustralia: "sport", datagolf: "sport", espn: "sport",
  laliga: "sport", mlb: "sport", nba: "sport", nrl: "sport", openf1: "sport",
  premierleague: "sport", seriea: "sport", twitter: "sport",
  // gambling & odds
  betfair: "gambling", betr: "gambling", entain: "gambling", fanduel: "gambling",
  kalshi: "gambling", pinnacle: "gambling", pointsbet: "gambling",
  polymarket: "gambling", racingandsports: "gambling", sportsbet: "gambling",
  tab: "gambling", unibet: "gambling",
};

export interface EntitlementShape {
  sport_slots: number;
  gambling_slots: number;
  all_access: number; // 0/1
}

export interface AssignmentCheck {
  ok: boolean;
  error?: string;
  providers?: string[]; // de-duplicated, on success
}

// Validate a requested feed assignment against the entitlement's slot budget.
// all-access → anything known; otherwise each kind must fit its slot count.
export function validateAssignment(requested: string[], ent: EntitlementShape): AssignmentCheck {
  const providers = [...new Set(requested.map(String))];
  const unknown = providers.filter((p) => !(p in PROVIDER_KIND));
  if (unknown.length) return { ok: false, error: `unknown providers: ${unknown.join(", ")}` };
  if (ent.all_access === 1) return { ok: true, providers };
  const sport = providers.filter((p) => PROVIDER_KIND[p] === "sport").length;
  const gambling = providers.filter((p) => PROVIDER_KIND[p] === "gambling").length;
  if (sport > ent.sport_slots) {
    return { ok: false, error: `too many sport feeds: ${sport} assigned, ${ent.sport_slots} allowed` };
  }
  if (gambling > ent.gambling_slots) {
    return { ok: false, error: `too many gambling feeds: ${gambling} assigned, ${ent.gambling_slots} allowed` };
  }
  return { ok: true, providers };
}
