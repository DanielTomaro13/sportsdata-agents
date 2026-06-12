"""Licensing & entitlements (P4): signed tokens, tier gradient, enforcement."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from sportsdata_agents.licensing import entitlements as ents_mod
from sportsdata_agents.licensing import license as lic
from sportsdata_agents.licensing.enforce import (
    EntitlementError,
    cap_mcp_groups,
    filter_roster,
    require_addon,
    require_chat_ui,
    require_full_app,
)
from sportsdata_agents.licensing.entitlements import TIERS, entitlements_for_tier

pytestmark = pytest.mark.unit


@pytest.fixture()
def keypair() -> tuple[str, str]:
    return lic.generate_keypair()


def test_tier_gradient_is_monotonic() -> None:
    free, base, plus, pro = (TIERS[t] for t in ("free", "base", "plus", "pro"))
    assert free.mcp_quota < base.mcp_quota < plus.mcp_quota
    assert pro.mcp_quota == -1  # unlimited
    assert not free.chat_ui and not base.chat_ui and plus.chat_ui and pro.chat_ui
    assert not plus.full_app and pro.full_app
    assert plus.agents is not None and pro.agents is None  # pro = all agents


def test_sign_verify_round_trip(keypair: tuple[str, str]) -> None:
    priv, pub = keypair
    token = lic.issue_license(priv, tier="pro", issued_to="d@x", addons=["slack"], days=30)
    claims = lic.verify_license(token, public_key_b64=pub)
    assert claims.tier == "pro" and claims.addons == ("slack",)
    assert claims.expires == dt.date.today() + dt.timedelta(days=30)


def test_tampered_token_is_rejected(keypair: tuple[str, str]) -> None:
    priv, pub = keypair
    token = lic.issue_license(priv, tier="pro", issued_to="d@x", days=30)
    with pytest.raises(lic.LicenseError):
        lic.verify_license(token[:-6] + "AAAAAA", public_key_b64=pub)
    # a different key never validates
    _, other_pub = lic.generate_keypair()
    with pytest.raises(lic.LicenseError):
        lic.verify_license(token, public_key_b64=other_pub)


def test_expired_token_is_rejected(keypair: tuple[str, str]) -> None:
    priv, pub = keypair
    token = lic.issue_license(priv, tier="plus", issued_to="d@x", days=1)
    future = dt.date.today() + dt.timedelta(days=2)
    with pytest.raises(lic.LicenseError, match="expired"):
        lic.verify_license(token, public_key_b64=pub, today=future)


def test_no_public_key_means_free_tier(keypair: tuple[str, str]) -> None:
    """An unsigned/untrusted environment never grants a paid tier."""
    priv, _pub = keypair
    token = lic.issue_license(priv, tier="pro", issued_to="d@x", days=30)
    with pytest.raises(lic.LicenseError, match="no license public key"):
        lic.verify_license(token, public_key_b64="")


def test_load_license_degrades_to_free_on_bad_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPORTSDATA_LICENSE", "garbage.token")
    monkeypatch.setattr(lic, "LICENSE_PUBLIC_KEY_B64", "")
    assert lic.load_license() is None  # never raises; falls to free


def test_current_entitlements_uses_a_valid_license(
    keypair: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    priv, pub = keypair
    token = lic.issue_license(priv, tier="plus", issued_to="d@x", addons=["slack"], days=30)
    monkeypatch.setenv("SPORTSDATA_LICENSE", token)
    monkeypatch.setattr(lic, "LICENSE_PUBLIC_KEY_B64", pub)
    e = ents_mod.current_entitlements()
    assert e.tier == "plus" and e.chat_ui and e.has_addon("slack")


def test_extra_mcp_addon_raises_quota() -> None:
    base = entitlements_for_tier("base")
    assert base.effective_mcp_quota() == 5
    with_pack = entitlements_for_tier("base", frozenset({"extra_mcps"}))
    assert with_pack.effective_mcp_quota() == 10  # +5 per pack


def test_cap_mcp_groups_trims_to_quota() -> None:
    groups = [f"g{i}" for i in range(10)]
    free = entitlements_for_tier("free")  # quota 2
    assert cap_mcp_groups(groups, free) == ["g0", "g1"]
    pro = entitlements_for_tier("pro")  # unlimited
    assert cap_mcp_groups(groups, pro) == groups


def test_filter_roster_keeps_ops_and_entitled_product_agents() -> None:
    class _Spec:
        def __init__(self, plane: str = "product", cdt: list[str] | None = None) -> None:
            self.plane = plane
            self.can_delegate_to = cdt or []

        def model_copy(self, update: dict) -> Any:
            s = _Spec(self.plane, self.can_delegate_to)
            for k, v in update.items():
                setattr(s, k, v)
            return s

    specs = {
        "orchestrator": _Spec(cdt=["odds_specialist", "modelling", "arb_hunter"]),
        "odds_specialist": _Spec(),
        "modelling": _Spec(),          # not in the plus roster
        "arb_hunter": _Spec(),          # not in the plus roster
        "incident_triage": _Spec(plane="ops"),  # ops: always kept
    }
    plus = entitlements_for_tier("plus")  # agents = orchestrator/odds/stats/concierge
    kept = filter_roster(specs, "orchestrator", plus)
    assert set(kept) == {"orchestrator", "odds_specialist", "incident_triage"}
    # the orchestrator's delegate list was pruned to what survived
    assert kept["orchestrator"].can_delegate_to == ["odds_specialist"]


def test_enforce_gates_raise_below_tier() -> None:
    free = entitlements_for_tier("free")
    with pytest.raises(EntitlementError):
        require_chat_ui(free)
    with pytest.raises(EntitlementError):
        require_full_app(free)
    with pytest.raises(EntitlementError):
        require_addon("slack", free)
    pro = entitlements_for_tier("pro", frozenset({"slack"}))
    require_chat_ui(pro)
    require_full_app(pro)
    require_addon("slack", pro)  # no raise


def test_unlicensed_source_build_is_unrestricted_but_product_build_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No baked-in pubkey (source/server) → full access. A product build (pubkey
    set) with no valid license → free tier. This keeps `agents …` from source
    uncrippled while letting a shipped build enforce."""
    from sportsdata_agents.licensing import enforce

    for var in ("SPORTSDATA_LICENSE",):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(lic, "get_keychain_secret", lambda name: None, raising=False)
    import sportsdata_agents.secrets as secrets
    monkeypatch.setattr(secrets, "get_keychain_secret", lambda name: None)

    monkeypatch.setattr(lic, "LICENSE_PUBLIC_KEY_B64", "")  # source build
    assert ents_mod.current_entitlements().full_app is True  # unrestricted
    enforce.require_full_app()  # no raise

    monkeypatch.setattr(lic, "LICENSE_PUBLIC_KEY_B64", "somebakedkey")  # product build
    assert ents_mod.current_entitlements().tier == "free"
    with pytest.raises(enforce.EntitlementError):
        enforce.require_full_app()


def test_unknown_addons_are_ignored_defensively() -> None:
    """A mis-issued or future-renamed add-on never silently grants a feature —
    only catalogue add-ons survive resolution (signature stops injection; this
    stops our own typos)."""
    e = entitlements_for_tier("pro", frozenset({"slack", "god_mode", "extra_mcpz"}))
    assert e.has_addon("slack")
    assert not e.has_addon("god_mode") and not e.has_addon("extra_mcpz")
    assert e.addons == frozenset({"slack"})


def test_serve_chat_gate_is_independent_of_the_roster_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Base tier (no chat_ui) must not be able to run the chat gateway even
    though the roster filter would still scope it — the product gate is separate."""
    from sportsdata_agents.licensing.enforce import EntitlementError, require_chat_ui

    base = entitlements_for_tier("base")
    assert base.chat_ui is False
    with pytest.raises(EntitlementError):
        require_chat_ui(base)
