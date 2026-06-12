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

Delivery always journals the key to an audit file, and **emails it when SMTP is
configured** (``SMTP_HOST`` + friends, ``BILLING_FROM_EMAIL``); with no SMTP set
you send keys from the audit log manually. A send failure falls back to the log
and never 500s the webhook.
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
    try:  # a non-numeric ts is a forged/garbled header, not a server error
        ts_val = int(ts)
    except ValueError:
        return False
    if abs((now or time.time()) - ts_val) > SIGNATURE_MAX_AGE_S:
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


# ── delivery: audit log (always) + email (when SMTP is configured) ───────────


def smtp_config() -> dict[str, Any] | None:
    """SMTP settings from the env, or None when email delivery isn't configured.

    With no ``SMTP_HOST`` the webhook still works — the key lands in the audit log
    and you email it manually. Set host/port/user/password + ``BILLING_FROM_EMAIL``
    to have :func:`deliver_license` send it automatically."""
    host = os.environ.get("SMTP_HOST")
    if not host:
        return None
    user = os.environ.get("SMTP_USER", "")
    return {
        "host": host,
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "user": user,
        "password": os.environ.get("SMTP_PASSWORD", ""),
        "from_addr": os.environ.get("BILLING_FROM_EMAIL") or user,
        "starttls": os.environ.get("SMTP_STARTTLS", "1") != "0",
    }


def _license_email_body(token: str, plan: dict[str, Any]) -> str:
    tier = plan.get("tier", "")
    addons = ", ".join(plan.get("addons") or []) or "none"
    return (
        "Thanks for subscribing to sportsdata.\n\n"
        f"Your licence ({tier}, add-ons: {addons}):\n\n{token}\n\n"
        "Activate it on the machine where you installed the app:\n\n"
        "    agents license --activate <the key above>\n\n"
        "It verifies offline — nothing phones home. Keep this email; re-activate "
        "anytime with the same key.\n"
    )


def send_license_email(cfg: dict[str, Any], to_addr: str, token: str, plan: dict[str, Any]) -> None:
    """Send the issued key over SMTP (STARTTLS by default). Raises on transport
    failure — :func:`deliver_license` is responsible for swallowing it."""
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = cfg["from_addr"]
    msg["To"] = to_addr
    msg["Subject"] = "Your sportsdata licence key"
    msg.set_content(_license_email_body(token, plan))
    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=20) as server:
        if cfg["starttls"]:
            server.starttls()
        if cfg["user"]:
            server.login(cfg["user"], cfg["password"])
        server.send_message(msg)


def deliver_license(email: str, token: str, *, plan: dict[str, Any]) -> None:
    """Get the key to the buyer: always journal it to the audit file, and email it
    when SMTP is configured. Never raises — a delivery failure must not 500 the
    webhook (the processor would retry forever); the audit log is the backstop."""
    from sportsdata_agents.paths import data_dir

    try:
        line = json.dumps({
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "email": email, "tier": plan.get("tier"), "addons": plan.get("addons"),
            "token": token,
        })
        with (data_dir() / "issued-licenses.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as e:
        logger.error("license audit-log write failed: %s", e)

    cfg = smtp_config()
    if cfg is None:
        logger.info("license issued for %s (%s) — SMTP not set, deliver from the audit log",
                    email, plan.get("tier"))
        return
    try:
        send_license_email(cfg, email, token, plan)
        logger.info("license emailed to %s (%s)", email, plan.get("tier"))
    except Exception as e:
        logger.error("license email to %s failed (key is in the audit log): %s", email, e)


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
        provider: str,
        request: Request,
        verify: Callable[[bytes, str, str], bool],
        sig_header: str,
    ) -> JSONResponse:
        body = await request.body()
        secret = os.environ.get(f"{provider.upper()}_WEBHOOK_SECRET", "")
        # each provider's OWN header only — never accept one provider's signature
        # scheme on another's endpoint
        header = request.headers.get(sig_header, "")
        if not verify(body, header, secret):
            return JSONResponse({"ok": False, "error": "bad signature"}, status_code=401)
        try:
            payload = json.loads(body)
        except ValueError:  # signed-but-garbled body: reject cleanly, never 500
            return JSONResponse({"ok": False, "error": "body is not valid JSON"}, status_code=400)
        try:
            token = handle_event(provider, payload)
        except BillingError as e:
            logger.error("billing error (%s): %s", provider, e)
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "issued": bool(token)})

    @app.post("/webhook/lemonsqueezy")
    async def ls(request: Request) -> JSONResponse:
        return await _process("lemonsqueezy", request, verify_lemonsqueezy, "x-signature")

    @app.post("/webhook/paddle")
    async def pad(request: Request) -> JSONResponse:
        return await _process("paddle", request, verify_paddle, "paddle-signature")

    @app.post("/licence/refresh")
    async def refresh(request: Request) -> JSONResponse:
        """{token} → the LATEST licence issued to the same buyer (renewal pickup).

        Auth is the customer's existing token itself: the signature must verify
        (expiry ignored — a lapsed-but-genuine token identifies a real customer).
        The newest audit-log entry for that email is returned; a cancelled
        subscriber simply gets back a token that expired with their last paid
        period, so this can never extend access — only deliver what renewals
        already minted. This is what makes short monthly tokens frictionless:
        `agents license --refresh` instead of pasting a new key every cycle."""
        from .license import verify_license

        pub = os.environ.get("SPORTSDATA_LICENSE_PUBKEY", "")
        try:
            body = json.loads(await request.body())
        except ValueError:
            return JSONResponse({"ok": False, "error": "body is not valid JSON"}, status_code=400)
        presented = str(body.get("token", "")).strip()
        if not presented:
            return JSONResponse({"ok": False, "error": "a licence token is required"}, status_code=422)
        try:
            claims = verify_license(presented, public_key_b64=pub or None, allow_expired=True)
        except Exception:
            return JSONResponse({"ok": False, "error": "token does not verify"}, status_code=401)

        from sportsdata_agents.paths import data_dir

        latest: str | None = None
        audit = data_dir() / "issued-licenses.jsonl"
        if audit.is_file():
            for line in audit.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("email") == claims.issued_to and rec.get("token"):
                    latest = str(rec["token"])  # the file is append-ordered: last wins
        if not latest:
            return JSONResponse({"ok": False, "error": "no issued licence on record"}, status_code=404)
        return JSONResponse({"ok": True, "token": latest})

    return app
