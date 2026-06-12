"""OTA data feed (P4 M4.5): sign → fetch → verify → apply → loaders see the overlay."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sportsdata_agents.licensing import license as lic
from sportsdata_agents.operations import datafeed

pytestmark = pytest.mark.unit


@pytest.fixture()
def keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, str]:
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATA_DIR", str(tmp_path))
    return lic.generate_keypair()  # (priv, pub)


def test_sign_verify_roundtrip_and_tamper(keys: tuple[str, str]) -> None:
    priv, pub = keys
    bundle = datafeed.build_bundle("1.2.3")
    sig = datafeed.sign_bundle(bundle, priv)
    assert datafeed.verify_bundle(bundle, sig, pub)
    # a wrong key fails
    _, other_pub = lic.generate_keypair()
    assert not datafeed.verify_bundle(bundle, sig, other_pub)
    # tampering with the content invalidates the signature
    bundle["files"]["market_dictionary"]["content"] += " "
    assert not datafeed.verify_bundle(bundle, sig, pub)


def test_build_bundle_contains_known_files(keys: tuple[str, str]) -> None:
    bundle = datafeed.build_bundle("9")
    assert set(bundle["files"]) == {"market_dictionary", "capability_labels"}
    for rec in bundle["files"].values():
        assert rec["sha256"] and rec["content"]


def test_apply_writes_overlay_and_data_text_prefers_it(keys: tuple[str, str], tmp_path: Path) -> None:
    bundle = datafeed.build_bundle("5")
    # mutate the market dictionary content so the overlay differs from packaged
    new = json.loads(bundle["files"]["market_dictionary"]["content"])
    new.setdefault("markets", {}).setdefault("h2h", []).append("ota-only-alias")
    text = json.dumps(new)
    import hashlib

    bundle["files"]["market_dictionary"] = {
        "sha256": hashlib.sha256(text.encode()).hexdigest(), "content": text,
    }

    out = datafeed.apply_bundle(bundle)
    assert set(out["applied"]) == {"market_dictionary", "capability_labels"}
    assert out["version"] == "5"
    assert datafeed.applied_version() == "5"
    # data_text now returns the overlay, not the packaged seed
    assert "ota-only-alias" in datafeed.data_text("market_dictionary")
    assert "ota-only-alias" not in datafeed.packaged_text("market_dictionary")


def test_apply_rejects_sha_mismatch(keys: tuple[str, str]) -> None:
    bundle = datafeed.build_bundle("1")
    bundle["files"]["market_dictionary"]["content"] += "corruption"  # sha now stale
    with pytest.raises(datafeed.DataFeedError, match="sha256 mismatch"):
        datafeed.apply_bundle(bundle)


def test_apply_rejects_bad_schema(keys: tuple[str, str]) -> None:
    bundle = datafeed.build_bundle("1")
    bundle["schema"] = 999
    with pytest.raises(datafeed.DataFeedError, match="schema"):
        datafeed.apply_bundle(bundle)


def test_apply_skips_unknown_files(keys: tuple[str, str]) -> None:
    bundle = datafeed.build_bundle("1")
    bundle["files"]["totally_unknown"] = {"sha256": "x", "content": "y"}
    out = datafeed.apply_bundle(bundle)  # unknown skipped, not fatal
    assert "totally_unknown" not in out["applied"]


def test_fetch_and_apply_verifies_signature(keys: tuple[str, str], tmp_path: Path) -> None:
    priv, pub = keys
    bundle = datafeed.build_bundle("7")
    doc = {"bundle": bundle, "signature": datafeed.sign_bundle(bundle, priv)}
    feed = tmp_path / "data-bundle.json"
    feed.write_text(json.dumps(doc))
    url = feed.as_uri()

    out = datafeed.fetch_and_apply(url, public_key_b64=pub)
    assert out["version"] == "7"

    # a forged bundle (re-signed file body but signature over the original) is refused
    forged = {"bundle": {**bundle, "version": "8"}, "signature": doc["signature"]}
    feed.write_text(json.dumps(forged))
    with pytest.raises(datafeed.DataFeedError, match="verification FAILED"):
        datafeed.fetch_and_apply(url, public_key_b64=pub)


def test_fetch_without_pubkey_applies_unverified(keys: tuple[str, str], tmp_path: Path) -> None:
    priv, _pub = keys
    bundle = datafeed.build_bundle("3")
    doc = {"bundle": bundle, "signature": datafeed.sign_bundle(bundle, priv)}
    feed = tmp_path / "b.json"
    feed.write_text(json.dumps(doc))
    # empty pubkey = source/dev build: applies with a warning, no exception
    out = datafeed.fetch_and_apply(feed.as_uri(), public_key_b64="")
    assert out["version"] == "3"


def test_canonical_market_uses_applied_overlay(keys: tuple[str, str]) -> None:
    """The runtime normalizer canonicalizes via the overlay once applied."""
    from sportsdata_agents.operations.ingestion import normalizers

    bundle = datafeed.build_bundle("1")
    base = json.loads(bundle["files"]["market_dictionary"]["content"])
    base.setdefault("markets", {})["h2h"] = [*base.get("markets", {}).get("h2h", []), "funky_book_name"]
    text = json.dumps(base)
    import hashlib

    bundle["files"]["market_dictionary"] = {
        "sha256": hashlib.sha256(text.encode()).hexdigest(), "content": text,
    }
    datafeed.apply_bundle(bundle)  # apply_bundle reloads the dictionary cache
    assert normalizers.canonical_market("funky_book_name") == "h2h"
