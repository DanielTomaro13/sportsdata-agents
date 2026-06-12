"""Offline-verifiable license tokens (Ed25519).

A license is ``<base64url(payload)>.<base64url(signature)>`` — a signed JSON
claims blob. The PUBLIC key ships in the binary (verification needs no
network); the PRIVATE key issues licenses (an ops/payment-webhook secret,
never in the app). Verification failures fail OPEN to the free tier — a broken
license must never lock a paying user out harder than not having one.

Resolution order for the running install:
1. ``SPORTSDATA_LICENSE`` env (CI/dev),
2. the OS keychain (where the wizard stores the user's key),
3. ``<data_dir>/license.key`` (a file the user can drop in),
4. none → free tier.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# The product's license-verification public key. Replace with the real key at
# release; the matching private key issues licenses (see scripts/license.py).
# A placeholder key means "no signature trusted" → every install is free tier,
# which is the correct safe default before a real keypair is generated.
LICENSE_PUBLIC_KEY_B64 = os.environ.get("SPORTSDATA_LICENSE_PUBKEY", "")

KEYCHAIN_LICENSE_NAME = "SPORTSDATA_LICENSE"


class LicenseError(RuntimeError):
    """A license token was present but invalid (bad signature, shape, or expiry)."""


@dataclass(frozen=True)
class LicenseClaims:
    tier: str
    addons: tuple[str, ...]
    seats: int
    issued_to: str
    expires: dt.date | None
    raw: dict
    operator: bool = False
    """True only on a token the product owner signed for themselves (the
    cryptographic operator grant). Customer tokens never carry it — minting one
    needs the private key, which never ships. See ``scheduler.is_operator``."""


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def verify_license(
    token: str,
    *,
    public_key_b64: str | None = None,
    today: dt.date | None = None,
    allow_expired: bool = False,
) -> LicenseClaims:
    """Verify a token's signature + expiry and return its claims, or raise.

    No trusted public key (placeholder/empty) → raise, so the caller falls to
    free tier rather than trusting an unsigned blob. ``allow_expired`` skips ONLY
    the expiry check (signature still mandatory) — the refresh endpoint uses it
    to recognise a lapsed-but-genuine customer token."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    pub_b64 = public_key_b64 if public_key_b64 is not None else LICENSE_PUBLIC_KEY_B64
    if not pub_b64:
        raise LicenseError("no license public key configured — running unlicensed")
    try:
        payload_b64, sig_b64 = token.strip().split(".", 1)
        payload = _b64url_decode(payload_b64)
        signature = _b64url_decode(sig_b64)
    except Exception as e:  # malformed token (split/decode/etc)
        raise LicenseError(f"malformed license token: {e}") from e

    try:
        Ed25519PublicKey.from_public_bytes(_b64url_decode(pub_b64)).verify(signature, payload)
    except (InvalidSignature, ValueError) as e:
        raise LicenseError("license signature does not verify") from e

    claims = json.loads(payload)
    expires_raw = claims.get("expires")
    expires = dt.date.fromisoformat(expires_raw) if expires_raw else None
    if expires is not None and not allow_expired and (today or dt.date.today()) > expires:
        raise LicenseError(f"license expired on {expires}")
    if claims.get("tier") not in ("base", "plus", "pro"):
        raise LicenseError(f"unknown tier {claims.get('tier')!r}")
    return LicenseClaims(
        tier=str(claims["tier"]),
        addons=tuple(claims.get("addons") or []),
        seats=int(claims.get("seats", 1)),
        issued_to=str(claims.get("issued_to", "")),
        expires=expires,
        raw=claims,
        operator=claims.get("operator") is True,
    )


def _token_from_sources() -> str | None:
    if os.environ.get("SPORTSDATA_LICENSE"):
        return os.environ["SPORTSDATA_LICENSE"]
    from sportsdata_agents.secrets import get_keychain_secret

    kc = get_keychain_secret(KEYCHAIN_LICENSE_NAME)
    if kc:
        return kc
    from sportsdata_agents.paths import data_dir

    key_file = data_dir() / "license.key"
    if key_file.is_file():
        return key_file.read_text(encoding="utf-8").strip()
    return None


def load_license(today: dt.date | None = None) -> LicenseClaims | None:
    """The verified claims for the running install, or None (free tier).
    Never raises — a bad license logs once and degrades to free."""
    token = _token_from_sources()
    if not token:
        return None
    try:
        return verify_license(token, today=today)
    except LicenseError as e:
        logger.warning("license invalid (%s) — running on the free tier", e)
        return None


def issue_license(
    private_key_b64: str,
    *,
    tier: str,
    issued_to: str,
    addons: list[str] | None = None,
    seats: int = 1,
    days: int | None = 365,
    operator: bool = False,
) -> str:
    """Mint a signed license token (the issuer side — payment webhook / ops).
    Lives here so the format has one definition; the private key never ships.

    ``operator=True`` stamps the cryptographic operator grant. ONLY the product
    owner runs this (they hold the private key), so it's the unforgeable basis
    for ``scheduler.is_operator`` on a release build — a customer cannot mint it."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if addons:
        from .entitlements import ADDONS

        unknown = [a for a in addons if a not in ADDONS]
        if unknown:
            logger.warning("issuing a license with unknown add-on(s) %s — they will be IGNORED "
                           "at resolution; check the spelling against ADDONS", unknown)
    payload = {
        "tier": tier,
        "issued_to": issued_to,
        "addons": addons or [],
        "seats": seats,
        "issued": dt.date.today().isoformat(),
        "expires": (dt.date.today() + dt.timedelta(days=days)).isoformat() if days else None,
    }
    if operator:  # the cryptographic operator grant — omitted on ordinary tokens
        payload["operator"] = True
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    key = Ed25519PrivateKey.from_private_bytes(_b64url_decode(private_key_b64))
    signature = key.sign(payload_bytes)
    return f"{_b64url_encode(payload_bytes)}.{_b64url_encode(signature)}"


def generate_keypair() -> tuple[str, str]:
    """(private_b64, public_b64) — run ONCE to create the product's signing key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    priv_b64 = _b64url_encode(
        private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    pub_b64 = _b64url_encode(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
    )
    return priv_b64, pub_b64
