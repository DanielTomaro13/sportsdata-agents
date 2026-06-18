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
