"""Payment-webhook → license issuance (the ONE small server P4 needs).

Provider-agnostic: the core maps a purchase to a tier and mints a signed license
(:func:`issue_license`); thin adapters handle each processor's signature scheme
and event shape. Today: **Paddle** (Billing) and **LemonSqueezy** — both
merchant-of-record, so they collect GST/VAT and we never touch card data.

Run it with ``agents billing`` next to:
- ``SPORTSDATA_LICENSE_PRIVKEY`` — the signing private key (from `license.py keygen`),
- ``SPORTSDATA_BILLING_PRODUCTS`` — JSON mapping each processor's product/variant
  id to ``{tier, addons, days}``,
- ``PADDLE_WEBHOOK_SECRET`` / ``LEMONSQUEEZY_WEBHOOK_SECRET`` — the signing secrets.

Delivery (emailing the key to the buyer) is a pluggable seam; the default logs
and appends to an audit file. Wiring a real email provider is a POST_DEV step.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Imported at module level so FastAPI can resolve the route annotations under
# `from __future__ import annotations` (string annotations are looked up in the
# module globals, not the create_billing_app() closure).
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

SIGNATURE_MAX_AGE_S = 5 * 60  # reject replayed/old Paddle signatures


class BillingError(RuntimeError):
    """A webhook could not be processed (bad signature, unknown product, config)."""


# ── product → entitlement map (operator-configured) ──────────────────────────


def product_map() -> dict[str, dict[str, dict[str, Any]]]:
    """``{provider: {product_id: {tier, addons, days}}}`` from the env JSON."""
    raw = os.environ.get("SPORTSDATA_BILLING_PRODUCTS", "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError as e:
        raise BillingError(f"SPORTSDATA_BILLING_PRODUCTS is not valid JSON: {e}") from e


def plan_for_product(provider: str, product_id: str) -> dict[str, Any] | None:
    return product_map().get(provider, {}).get(str(product_id))


# ── signature verification (per processor) ───────────────────────────────────


def verify_lemonsqueezy(body: bytes, signature: str, secret: str) -> bool:
    """LemonSqueezy ``X-Signature`` = hex HMAC-SHA256 of the raw body."""
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def verify_paddle(body: bytes, sig_header: str, secret: str, *, now: float | None = None) -> bool:
    """Paddle ``Paddle-Signature: ts=…;h1=…`` — HMAC-SHA256 of ``ts:body``,
    with a freshness window to defeat replay."""
    if not secret or not sig_header:
        return False
    parts = dict(p.split("=", 1) for p in sig_header.split(";") if "=" in p)
    ts, h1 = parts.get("ts"), parts.get("h1")
    if not ts or not h1:
        return False
    if abs((now or time.time()) - int(ts)) > SIGNATURE_MAX_AGE_S:
        return False
    signed = f"{ts}:".encode() + body
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, h1)


# ── event → purchase extraction (per processor) ──────────────────────────────


@dataclass(frozen=True)
class Purchase:
    provider: str
    email: str
    product_id: str
    event: str


def extract_lemonsqueezy(payload: dict[str, Any]) -> Purchase | None:
    """A new/active subscription or completed order → a Purchase; else None."""
    name = (payload.get("meta") or {}).get("event_name", "")
    if name not in ("order_created", "subscription_created", "subscription_updated"):
        return None
    attrs = ((payload.get("data") or {}).get("attributes")) or {}
    if name.startswith("subscription") and attrs.get("status") not in ("active", "on_trial"):
        return None
    product_id = str(attrs.get("variant_id") or attrs.get("product_id") or "")
    email = str(attrs.get("user_email") or attrs.get("customer_email") or "")
    if not product_id or not email:
        return None
    return Purchase("lemonsqueezy", email, product_id, name)


def extract_paddle(payload: dict[str, Any]) -> Purchase | None:
    """Paddle Billing ``subscription.activated`` / ``transaction.completed``."""
    event = payload.get("event_type", "")
    if event not in ("subscription.activated", "subscription.created", "transaction.completed"):
        return None
    data = payload.get("data") or {}
    items = data.get("items") or []
    price = (items[0].get("price") if items else {}) or {}
    product_id = str(price.get("product_id") or price.get("id") or "")
    email = str((data.get("customer") or {}).get("email") or data.get("customer_email") or "")
    if not product_id or not email:
        return None
    return Purchase("paddle", email, product_id, event)


# ── delivery (pluggable; default = audit log) ────────────────────────────────


def deliver_license(email: str, token: str, *, plan: dict[str, Any]) -> None:
    """Get the key to the buyer. The default appends to an audit file and logs;
    a real email integration replaces this (POST_DEV). Never raises — a delivery
    failure must not 500 the webhook (the processor would retry forever)."""
    from sportsdata_agents.paths import data_dir

    try:
        line = json.dumps({
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "email": email, "tier": plan.get("tier"), "addons": plan.get("addons"),
            "token": token,
        })
        with (data_dir() / "issued-licenses.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        logger.info("license issued for %s (%s) — deliver via email (POST_DEV)", email, plan.get("tier"))
    except Exception as e:
        logger.error("license delivery side-effect failed: %s", e)


# ── the agnostic handler ─────────────────────────────────────────────────────


def handle_event(provider: str, payload: dict[str, Any], *, private_key: str | None = None) -> str | None:
    """Map a verified webhook event to a minted+delivered license token, or None
    for events that aren't a purchase. Raises BillingError on misconfig."""
    extract = {"lemonsqueezy": extract_lemonsqueezy, "paddle": extract_paddle}.get(provider)
    if extract is None:
        raise BillingError(f"unknown provider {provider!r}")
    purchase = extract(payload)
    if purchase is None:
        return None  # not a purchase event — ack and ignore

    plan = plan_for_product(provider, purchase.product_id)
    if plan is None:
        raise BillingError(f"no plan mapped for {provider} product {purchase.product_id!r} "
                           "(set SPORTSDATA_BILLING_PRODUCTS)")

    priv = private_key or os.environ.get("SPORTSDATA_LICENSE_PRIVKEY")
    if not priv:
        raise BillingError("SPORTSDATA_LICENSE_PRIVKEY not set — cannot sign a license")

    from .license import issue_license

    token = issue_license(
        priv,
        tier=str(plan["tier"]),
        issued_to=purchase.email,
        addons=list(plan.get("addons") or []),
        days=int(plan["days"]) if plan.get("days") is not None else None,
    )
    deliver_license(purchase.email, token, plan=plan)
    return token


def create_billing_app() -> FastAPI:
    """A tiny FastAPI app exposing the two webhook routes + /healthz."""
    app = FastAPI(title="sportsdata billing", version="1")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "providers": list(product_map().keys())}

    async def _process(
        provider: str, request: Request, verify: Callable[[bytes, str, str], bool]
    ) -> JSONResponse:
        body = await request.body()
        secret = os.environ.get(f"{provider.upper()}_WEBHOOK_SECRET", "")
        header = request.headers.get("paddle-signature") or request.headers.get("x-signature") or ""
        if not verify(body, header, secret):
            return JSONResponse({"ok": False, "error": "bad signature"}, status_code=401)
        try:
            token = handle_event(provider, json.loads(body))
        except BillingError as e:
            logger.error("billing error (%s): %s", provider, e)
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "issued": bool(token)})

    @app.post("/webhook/lemonsqueezy")
    async def ls(request: Request) -> JSONResponse:
        return await _process("lemonsqueezy", request, verify_lemonsqueezy)

    @app.post("/webhook/paddle")
    async def pad(request: Request) -> JSONResponse:
        return await _process("paddle", request, verify_paddle)

    return app
