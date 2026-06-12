"""OTA data updates (P4 M4.5) — refresh the data plane's DATA between releases.

The market dictionary and capability-label catalogue are DATA, not code: books
rename markets, add competitions, expose new capabilities. Shipping a whole new
app build for a dictionary tweak is the slow path the desktop model would
otherwise impose. This packages those files as a **signed bundle** a running app
fetches and applies into an **overlay** under the data dir — which the loaders
prefer over the packaged seed.

Trust model mirrors the licence: an Ed25519 detached signature over the canonical
bundle, verified offline against a baked ``SPORTSDATA_DATA_PUBKEY``. No baked key
(source/dev build) → unverified apply is allowed with a loud warning; a product
build with a key REFUSES an unsigned/forged bundle. The only network call is the
fetch the user explicitly asked for (``agents update-data``).

Publish with ``scripts/publish-data-bundle.py`` (signs with the matching private
key), upload the result as a release asset, point ``SPORTSDATA_DATA_FEED_URL`` at it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from importlib import resources
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA = 1
DATA_PUBKEY_ENV = "SPORTSDATA_DATA_PUBKEY"

# logical name → (package, resource) of the packaged default for each updatable file.
# Both are plain JSON read once at load; the book CATALOGUE (a skills-dir path with a
# separate package-write concern) is intentionally not here yet.
_PACKAGED: dict[str, tuple[str, str]] = {
    "market_dictionary": ("sportsdata_agents.operations.resolution", "market_dictionary.json"),
    "capability_labels": ("sportsdata_agents.agents", "capability_labels.json"),
}


class DataFeedError(RuntimeError):
    """A data bundle could not be verified or applied (bad signature, sha, schema)."""


# ── the overlay seam loaders consult ─────────────────────────────────────────


def _overlay_dir() -> Path:
    from sportsdata_agents.paths import data_dir

    return data_dir() / "data-overlay"


def packaged_text(name: str) -> str:
    """The packaged default for ``name`` (the build-time seed)."""
    pkg, res = _PACKAGED[name]
    return resources.files(pkg).joinpath(res).read_text(encoding="utf-8")


def data_text(name: str) -> str:
    """The live text for an updatable data file: the applied OTA overlay if one
    exists, else the packaged default. THIS is the single seam loaders call."""
    if name not in _PACKAGED:
        raise KeyError(f"unknown data file {name!r}; known: {sorted(_PACKAGED)}")
    overlay = _overlay_dir() / f"{name}.json"
    if overlay.is_file():
        try:
            return overlay.read_text(encoding="utf-8")
        except OSError as e:  # a corrupt overlay must never take the data plane down
            logger.warning("data overlay %s unreadable, using packaged default: %s", name, e)
    return packaged_text(name)


def applied_version() -> str | None:
    """The version of the currently-applied overlay, or None (running packaged)."""
    marker = _overlay_dir() / "VERSION"
    return marker.read_text(encoding="utf-8").strip() if marker.is_file() else None


# ── bundle build / sign / verify (Ed25519, reusing the licence b64 helpers) ──


def _canonical_bytes(bundle: dict[str, Any]) -> bytes:
    return json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode("utf-8")


def build_bundle(version: str) -> dict[str, Any]:
    """Package the current PACKAGED data files into a bundle dict (publish side)."""
    files: dict[str, Any] = {}
    for name in _PACKAGED:
        content = packaged_text(name)
        files[name] = {"sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(), "content": content}
    return {"schema": SCHEMA, "version": str(version), "files": files}


def sign_bundle(bundle: dict[str, Any], private_key_b64: str) -> str:
    """Detached Ed25519 signature (b64url) over the canonical bundle bytes."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from sportsdata_agents.licensing.license import _b64url_decode, _b64url_encode

    key = Ed25519PrivateKey.from_private_bytes(_b64url_decode(private_key_b64))
    return _b64url_encode(key.sign(_canonical_bytes(bundle)))


def verify_bundle(bundle: dict[str, Any], signature_b64: str, public_key_b64: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from sportsdata_agents.licensing.license import _b64url_decode

    try:
        Ed25519PublicKey.from_public_bytes(_b64url_decode(public_key_b64)).verify(
            _b64url_decode(signature_b64), _canonical_bytes(bundle)
        )
        return True
    except (InvalidSignature, ValueError):
        return False


# ── apply (client side) ──────────────────────────────────────────────────────


def apply_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """Write each file in a verified bundle to the overlay dir (sha256-checked).
    Unknown files are skipped (forward-compatible). Returns what was applied."""
    if bundle.get("schema") != SCHEMA:
        raise DataFeedError(f"unsupported bundle schema {bundle.get('schema')!r} (expected {SCHEMA})")
    overlay = _overlay_dir()
    overlay.mkdir(parents=True, exist_ok=True)
    applied: list[str] = []
    for name, rec in (bundle.get("files") or {}).items():
        if name not in _PACKAGED:
            logger.warning("bundle carries unknown data file %r — skipping", name)
            continue
        content = str(rec.get("content", ""))
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != rec.get("sha256"):
            raise DataFeedError(f"sha256 mismatch for {name} — refusing to apply a corrupt bundle")
        (overlay / f"{name}.json").write_text(content, encoding="utf-8")
        applied.append(name)
    (overlay / "VERSION").write_text(str(bundle.get("version", "")), encoding="utf-8")
    _reload_consumers()
    return {"applied": applied, "version": bundle.get("version")}


def _reload_consumers() -> None:
    """Drop in-process caches so an applied overlay takes effect immediately."""
    try:
        from sportsdata_agents.operations.ingestion.normalizers import reload_dictionary

        reload_dictionary()
    except Exception as e:
        logger.debug("dictionary reload after apply skipped: %s", e)


def fetch_and_apply(url: str, *, public_key_b64: str | None = None) -> dict[str, Any]:
    """Fetch ``{bundle, signature}`` from ``url``, verify, and apply. With a baked
    pubkey a bad signature is fatal; with none, applies UNVERIFIED with a warning
    (dev/source builds only — a product build always bakes the key)."""
    import urllib.request

    pub = public_key_b64 if public_key_b64 is not None else os.environ.get(DATA_PUBKEY_ENV, "")
    if url.startswith("http://"):
        # the signature protects integrity either way, but a plaintext feed lets a
        # network observer see WHAT data the install pulls — prefer https
        logger.warning("data feed over plain http (%s) — use https", url.split("?")[0])
    with urllib.request.urlopen(url, timeout=30) as resp:
        doc = json.loads(resp.read().decode("utf-8"))
    bundle, signature = doc.get("bundle"), doc.get("signature", "")
    if not isinstance(bundle, dict):
        raise DataFeedError("feed response missing a 'bundle' object")
    if pub:
        if not verify_bundle(bundle, signature, pub):
            raise DataFeedError("bundle signature verification FAILED — refusing to apply")
    else:
        logger.warning("no %s baked in — applying the data bundle UNVERIFIED (dev only)", DATA_PUBKEY_ENV)
    return apply_bundle(bundle)
