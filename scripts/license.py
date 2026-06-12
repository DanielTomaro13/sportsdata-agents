#!/usr/bin/env python3
"""License issuer (ops/payment side) — the private-key tool. NEVER ships in the app.

    python scripts/license.py keygen
        → prints a fresh (private, public) keypair. Run ONCE. Bake the public key
          into the build as SPORTSDATA_LICENSE_PUBKEY; keep the private key in the
          payment webhook secret store only.

    SPORTSDATA_LICENSE_PRIVKEY=... python scripts/license.py issue \\
        --tier pro --to alice@example.com --addons slack,discord --days 365
        → prints a signed license key to give the customer.

The payment webhook calls sportsdata_agents.licensing.issue_license(...) directly
with the same private key; this CLI is for manual issuance and testing.
"""

from __future__ import annotations

import argparse
import os
import sys

from sportsdata_agents.licensing import issue_license
from sportsdata_agents.licensing.license import generate_keypair


def main() -> int:
    parser = argparse.ArgumentParser(description="sportsdata license issuer")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("keygen", help="generate a signing keypair (run once)")
    issue = sub.add_parser("issue", help="mint a signed license key")
    issue.add_argument("--tier", required=True, choices=["base", "plus", "pro"])
    issue.add_argument("--to", required=True, help="who the license is issued to")
    issue.add_argument("--addons", default="", help="comma-separated add-ons")
    issue.add_argument("--seats", type=int, default=1)
    issue.add_argument("--days", type=int, default=365, help="validity; 0 = perpetual")
    args = parser.parse_args()

    if args.cmd == "keygen":
        priv, pub = generate_keypair()
        print(f"PRIVATE (secret, webhook only):\n  SPORTSDATA_LICENSE_PRIVKEY={priv}\n")
        print(f"PUBLIC (bake into the build):\n  SPORTSDATA_LICENSE_PUBKEY={pub}")
        return 0

    priv = os.environ.get("SPORTSDATA_LICENSE_PRIVKEY")
    if not priv:
        print("error: set SPORTSDATA_LICENSE_PRIVKEY (from keygen) to issue", file=sys.stderr)
        return 1
    token = issue_license(
        priv,
        tier=args.tier,
        issued_to=args.to,
        addons=[a.strip() for a in args.addons.split(",") if a.strip()],
        seats=args.seats,
        days=args.days or None,
    )
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
