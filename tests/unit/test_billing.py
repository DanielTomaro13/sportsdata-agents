"""Payment webhook → license issuance (P4): signatures, mapping, the agnostic flow."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sportsdata_agents.licensing import billing
from sportsdata_agents.licensing import license as lic

pytestmark = pytest.mark.unit


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> tuple[str, str]:
    priv, pub = lic.generate_keypair()
    monkeypatch.setenv("SPORTSDATA_LICENSE_PRIVKEY", priv)
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPORTSDATA_BILLING_PRODUCTS", json.dumps({
        "lemonsqueezy": {"V1": {"tier": "pro", "addons": ["slack"], "days": 365}},
        "paddle": {"pro_pri": {"tier": "plus", "addons": [], "days": 365}},
    }))
    monkeypatch.setenv("LEMONSQUEEZY_WEBHOOK_SECRET", "ls-secret")
    monkeypatch.setenv("PADDLE_WEBHOOK_SECRET", "pd-secret")
    return priv, pub


def test_lemonsqueezy_signature() -> None:
    body = b'{"x":1}'
    sig = hmac.new(b"ls-secret", body, hashlib.sha256).hexdigest()
    assert billing.verify_lemonsqueezy(body, sig, "ls-secret")
    assert not billing.verify_lemonsqueezy(body, sig, "wrong")
    assert not billing.verify_lemonsqueezy(body, "deadbeef", "ls-secret")


def test_paddle_signature_and_replay_window() -> None:
    body = b'{"y":2}'
    ts = int(time.time())
    h1 = hmac.new(b"pd-secret", f"{ts}:".encode() + body, hashlib.sha256).hexdigest()
    header = f"ts={ts};h1={h1}"
    assert billing.verify_paddle(body, header, "pd-secret")
    # an old timestamp (beyond the freshness window) is rejected even if signed
    old_ts = ts - billing.SIGNATURE_MAX_AGE_S - 10
    old_h1 = hmac.new(b"pd-secret", f"{old_ts}:".encode() + body, hashlib.sha256).hexdigest()
    assert not billing.verify_paddle(body, f"ts={old_ts};h1={old_h1}", "pd-secret")


def test_event_extraction_filters_non_purchases() -> None:
    assert billing.extract_lemonsqueezy({"meta": {"event_name": "subscription_cancelled"}}) is None
    p = billing.extract_lemonsqueezy({
        "meta": {"event_name": "order_created"},
        "data": {"attributes": {"variant_id": "V1", "user_email": "a@b.com"}},
    })
    assert p and p.product_id == "V1" and p.email == "a@b.com"
    # paddle: a non-purchase event is ignored
    assert billing.extract_paddle({"event_type": "subscription.paused"}) is None


def test_handle_event_mints_and_delivers(env: tuple[str, str]) -> None:
    _priv, pub = env
    payload = {
        "meta": {"event_name": "order_created"},
        "data": {"attributes": {"variant_id": "V1", "user_email": "buyer@x.com"}},
    }
    token = billing.handle_event("lemonsqueezy", payload)
    assert token
    claims = lic.verify_license(token, public_key_b64=pub)
    assert claims.tier == "pro" and claims.issued_to == "buyer@x.com" and "slack" in claims.addons

    # the audit log received the issued license
    from sportsdata_agents.paths import data_dir
    audit = (data_dir() / "issued-licenses.jsonl").read_text()
    assert "buyer@x.com" in audit and token in audit


def test_unmapped_product_raises(env: tuple[str, str]) -> None:
    payload = {
        "meta": {"event_name": "order_created"},
        "data": {"attributes": {"variant_id": "UNKNOWN", "user_email": "x@y.com"}},
    }
    with pytest.raises(billing.BillingError, match="no plan mapped"):
        billing.handle_event("lemonsqueezy", payload)


def test_webhook_app_rejects_bad_signature_and_accepts_good(env: tuple[str, str]) -> None:
    client = TestClient(billing.create_billing_app())
    assert client.get("/healthz").json()["ok"] is True

    body = json.dumps({
        "meta": {"event_name": "order_created"},
        "data": {"attributes": {"variant_id": "V1", "user_email": "z@z.com"}},
    }).encode()
    # no/forged signature → 401, no license minted
    bad = client.post("/webhook/lemonsqueezy", content=body, headers={"x-signature": "nope"})
    assert bad.status_code == 401

    good_sig = hmac.new(b"ls-secret", body, hashlib.sha256).hexdigest()
    ok = client.post("/webhook/lemonsqueezy", content=body, headers={"x-signature": good_sig})
    assert ok.status_code == 200 and ok.json()["issued"] is True
