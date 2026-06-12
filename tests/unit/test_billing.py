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


def test_paddle_malformed_ts_rejects_not_crashes() -> None:
    """A garbled signature header must be a clean reject (401 path), never a 500."""
    assert not billing.verify_paddle(b"{}", "ts=not-a-number;h1=deadbeef", "pd-secret")
    assert not billing.verify_paddle(b"{}", "ts=;h1=", "pd-secret")


def test_provider_signature_headers_are_not_interchangeable(env: tuple[str, str]) -> None:
    """Each endpoint accepts only its OWN provider's signature header."""
    client = TestClient(billing.create_billing_app())
    body = json.dumps({
        "meta": {"event_name": "order_created"},
        "data": {"attributes": {"variant_id": "V1", "user_email": "z@z.com"}},
    }).encode()
    good_sig = hmac.new(b"ls-secret", body, hashlib.sha256).hexdigest()
    # the right signature in the WRONG header is rejected
    r = client.post("/webhook/lemonsqueezy", content=body, headers={"paddle-signature": good_sig})
    assert r.status_code == 401


def test_signed_but_garbled_body_is_400_not_500(env: tuple[str, str]) -> None:
    client = TestClient(billing.create_billing_app())
    body = b"this is not json"
    sig = hmac.new(b"ls-secret", body, hashlib.sha256).hexdigest()
    r = client.post("/webhook/lemonsqueezy", content=body, headers={"x-signature": sig})
    assert r.status_code == 400 and "JSON" in r.json()["error"]


def test_licence_refresh_round_trip(env: tuple[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """The subscription loop: an EXPIRED-but-genuine token retrieves the latest
    renewal token for the same buyer; forged tokens and unknown buyers are refused."""
    priv, pub = env
    monkeypatch.setenv("SPORTSDATA_LICENSE_PUBKEY", pub)
    client = TestClient(billing.create_billing_app())

    old = lic.issue_license(priv, tier="pro", issued_to="sub@x.com", days=-1)  # lapsed
    new = lic.issue_license(priv, tier="pro", issued_to="sub@x.com", days=33)  # the renewal
    billing.deliver_license("sub@x.com", old, plan={"tier": "pro", "addons": []})
    billing.deliver_license("sub@x.com", new, plan={"tier": "pro", "addons": []})

    # presenting the lapsed token returns the LATEST issued one
    r = client.post("/licence/refresh", json={"token": old})
    assert r.status_code == 200 and r.json()["token"] == new

    # a forged token never gets a licence back
    other_priv, _ = lic.generate_keypair()
    forged = lic.issue_license(other_priv, tier="pro", issued_to="sub@x.com", days=33)
    assert client.post("/licence/refresh", json={"token": forged}).status_code == 401

    # a genuine token for a buyer with no audit record → 404
    stranger = lic.issue_license(priv, tier="base", issued_to="nobody@x.com", days=33)
    assert client.post("/licence/refresh", json={"token": stranger}).status_code == 404

    # no token → 422
    assert client.post("/licence/refresh", json={}).status_code == 422


def test_deliver_emails_when_smtp_configured(env: tuple[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
            captured["host"], captured["port"] = host, port

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def starttls(self) -> None:
            captured["tls"] = True

        def login(self, user: str, password: str) -> None:
            captured["login"] = (user, password)

        def send_message(self, msg: Any) -> None:
            captured["msg"] = msg

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "u@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    monkeypatch.setenv("BILLING_FROM_EMAIL", "billing@sportsdata.app")

    billing.deliver_license("buyer@x.com", "TOK.SIG", plan={"tier": "pro", "addons": ["slack"]})

    assert captured["host"] == "smtp.example.com" and captured["tls"] is True
    assert captured["login"] == ("u@example.com", "pw")
    msg = captured["msg"]
    assert msg["To"] == "buyer@x.com" and msg["From"] == "billing@sportsdata.app"
    assert "TOK.SIG" in msg.get_content() and "agents license --activate" in msg.get_content()


def test_deliver_without_smtp_only_audits(env: tuple[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    from sportsdata_agents.paths import data_dir

    monkeypatch.delenv("SMTP_HOST", raising=False)
    billing.deliver_license("a@b.com", "T.S", plan={"tier": "base", "addons": []})  # must not raise
    assert "a@b.com" in (data_dir() / "issued-licenses.jsonl").read_text()


def test_deliver_swallows_email_failure(env: tuple[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    from sportsdata_agents.paths import data_dir

    class BoomSMTP:
        def __init__(self, *a: object, **k: object) -> None:
            raise RuntimeError("connection refused")

    monkeypatch.setattr("smtplib.SMTP", BoomSMTP)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    # a dead SMTP must NOT raise — the key is still in the audit log to send manually
    billing.deliver_license("c@d.com", "T.S", plan={"tier": "plus", "addons": []})
    assert "c@d.com" in (data_dir() / "issued-licenses.jsonl").read_text()
