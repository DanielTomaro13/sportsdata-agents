#!/usr/bin/env python3
"""Create the sportsdata Stripe products, monthly prices, and hosted Payment Links,
then write ``site/stripe.json`` with the checkout URLs the marketing site reads.

The site is a static GitHub Pages page with no backend, so checkout is done with
Stripe-hosted **Payment Links** — no server, no card data ever touches us.

Run this ONCE in your own shell with your Stripe secret key in the env. Test first,
then live when you're ready — the SAME script, only the key changes:

    export STRIPE_SECRET_KEY=sk_test_xxx      # test mode (fake cards)
    python scripts/setup-stripe.py
    # …happy? then in your own shell, with the LIVE key you set in your host's secrets:
    export STRIPE_SECRET_KEY=sk_live_xxx
    python scripts/setup-stripe.py
    sh scripts/deploy-site.sh                  # publishes the site + stripe.json

It is idempotent: re-running reuses products/prices/links matched by metadata instead
of duplicating them. The secret key is read from the env and never stored or printed.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    import stripe
except ImportError:
    sys.exit("This needs the Stripe SDK:  pip install stripe   — then re-run.")

KEY = os.environ.get("STRIPE_SECRET_KEY")
if not KEY:
    sys.exit("Set STRIPE_SECRET_KEY in your env (sk_test_… or sk_live_…) and re-run.")
if KEY.startswith("sk_live_"):
    confirm = input("⚠️  This is a LIVE key — real charges. Type 'live' to proceed: ").strip()
    if confirm != "live":
        sys.exit("aborted.")
stripe.api_key = KEY

# sku → (display name, description, unit amount in CENTS, adjustable quantity?)
CATALOGUE: dict[str, tuple[str, str, int, bool]] = {
    "base": (
        "sportsdata Base",
        "Data plane MCP access — 5 sport data MCPs of your choice, in your own AI client.",
        1500, False,
    ),
    "sport_addon": (
        "sportsdata Sport MCP", "One extra sport data MCP beyond your first 5.", 500, True,
    ),
    "gambling_addon": (
        "sportsdata Gambling MCP",
        "One live bookmaker / odds MCP (Sportsbet, TAB, Betfair, Pinnacle…).",
        1500, True,
    ),
    "all_access": (
        "sportsdata All-access", "Every sport and gambling MCP — the whole catalogue.", 9900, False,
    ),
}


def find_product(sku: str):
    try:
        res = stripe.Product.search(query=f"metadata['sportsdata_sku']:'{sku}'", limit=1)
        return res.data[0] if res.data else None
    except Exception:  # search index can lag right after creation — fall through to create
        return None


def ensure_product(sku: str, name: str, desc: str):
    return find_product(sku) or stripe.Product.create(
        name=name, description=desc, metadata={"sportsdata_sku": sku}
    )


def ensure_price(product, amount: int):
    for pr in stripe.Price.list(product=product.id, active=True, limit=20).data:
        if pr.unit_amount == amount and pr.recurring and pr.recurring.interval == "month":
            return pr
    return stripe.Price.create(
        product=product.id, unit_amount=amount, currency="usd", recurring={"interval": "month"}
    )


def ensure_link(price, adjustable: bool):
    for link in stripe.PaymentLink.list(active=True, limit=100).data:
        items = stripe.PaymentLink.list_line_items(link.id, limit=10).data
        if any(it.price.id == price.id for it in items):
            return link
    item: dict = {"price": price.id, "quantity": 1}
    if adjustable:
        item["adjustable_quantity"] = {"enabled": True, "minimum": 1, "maximum": 20}
    return stripe.PaymentLink.create(line_items=[item], allow_promotion_codes=True)


def main() -> None:
    mode = "LIVE" if KEY.startswith("sk_live_") else "test"
    print(f"Stripe mode: {mode}\n")
    links: dict[str, str] = {}
    for sku, (name, desc, amount, adjustable) in CATALOGUE.items():
        product = ensure_product(sku, name, desc)
        price = ensure_price(product, amount)
        link = ensure_link(price, adjustable)
        links[sku] = link.url
        print(f"  {sku:16} {name:26} ${amount / 100:>7.2f}/mo  {link.url}")

    out_path = Path(__file__).resolve().parents[1] / "site" / "stripe.json"
    out_path.write_text(json.dumps(links, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {out_path} ({mode} links).")
    print("Next:  sh scripts/deploy-site.sh   — publishes the site + stripe.json.")


if __name__ == "__main__":
    main()
