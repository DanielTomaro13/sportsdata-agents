"""The engines seam: a private-API coupling, made auditable.

`sportsdata-engines` is optional and private, versions independently, and is NOT
installed in this repo's environment or its CI — so nothing here imports it and nothing
catches a break until a pricing call fails at runtime in front of a user. These tests do
what can be done without the package: pin the coupling's shape, and prove the version
guard behaves.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from sportsdata_agents.quant.engines_contract import (
    ALLOWED_IMPORTERS,
    EXPECTED_SYMBOLS,
    MIN_ENGINES_VERSION,
    check_version,
    installed_version,
)

pytestmark = pytest.mark.unit

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "sportsdata_agents"


def _engines_imports() -> dict[str, set[str]]:
    """Every `sportsdata_engines` symbol this repo imports, read from the AST.

    Parsed rather than grepped so a commented-out import or a mention in a docstring
    does not count as a coupling.
    """
    found: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("sportsdata_engines"):
                found.setdefault(node.module or "", set()).update(a.name for a in node.names)
    return found


def test_the_engines_import_surface_matches_the_declared_contract() -> None:
    """The 21 call sites reach into engines internals — `racing.infer`, `ratings.footy`,
    `replay`, `core.staking` — most bypassing the `price_board_any` seam built to prevent
    exactly that. Engines owes those symbols nothing; an upgrade may move any of them.

    This does not stop that. It makes it a DECLARED coupling: adding a 22nd, or an
    engines refactor that lands here, updates this list deliberately instead of silently.
    """
    actual = _engines_imports()
    expected = {mod: set(names) for mod, names in EXPECTED_SYMBOLS.items()}

    undeclared = {
        mod: sorted(names - expected.get(mod, set()))
        for mod, names in actual.items()
        if names - expected.get(mod, set())
    }
    assert not undeclared, (
        "new coupling into sportsdata_engines internals — add it to EXPECTED_SYMBOLS in "
        f"quant/engines_contract.py, deliberately: {undeclared}"
    )

    gone = {mod: sorted(names) for mod, names in expected.items() if mod not in actual}
    assert not gone, (
        f"declared engines couplings no longer imported anywhere — drop them from "
        f"EXPECTED_SYMBOLS so the inventory stays honest: {gone}"
    )


def test_only_known_modules_reach_into_engines() -> None:
    """Keeping the coupling in a known set is what makes it auditable. A new file
    reaching into engines should be a decision, not a discovery."""
    importers = sorted(
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if any(
            isinstance(n, ast.ImportFrom) and (n.module or "").startswith("sportsdata_engines")
            for n in ast.walk(ast.parse(path.read_text(), str(path)))
        )
    )
    unexpected = [p for p in importers if p not in ALLOWED_IMPORTERS]
    assert not unexpected, (
        f"these modules import sportsdata_engines but are not in ALLOWED_IMPORTERS: {unexpected}"
    )


def test_the_version_guard_is_quiet_when_no_engine_is_installed() -> None:
    """The normal case. The platform runs fine without an engine — the seam reports
    unavailable — so absence must not warn, or the warning stops meaning anything."""
    if installed_version() is not None:
        pytest.skip("engines is installed in this environment")
    assert check_version() is None


def test_the_version_guard_warns_on_an_engine_older_than_the_call_sites_assume(caplog) -> None:
    """The failure it exists to catch: an engines old enough that symbols this repo
    imports have not arrived yet, which would otherwise surface as an ImportError deep
    inside a pricing call, naming a module the user has never heard of."""
    import sys
    import types

    stale = types.ModuleType("sportsdata_engines")
    stale.__version__ = "0.9.0"  # type: ignore[attr-defined]
    sys.modules["sportsdata_engines"] = stale
    try:
        with caplog.at_level("WARNING"):
            assert check_version() == "0.9.0"
        assert "older than the minimum" in caplog.text
        assert "SPORTSDATA_AGENTS_ENGINE_BACKEND=none" in caplog.text, (
            "the warning must say what to do about it, not just that it happened"
        )
    finally:
        del sys.modules["sportsdata_engines"]


def test_a_current_engine_passes_without_warning(caplog) -> None:
    import sys
    import types

    current = types.ModuleType("sportsdata_engines")
    current.__version__ = ".".join(str(p) for p in MIN_ENGINES_VERSION)  # type: ignore[attr-defined]
    sys.modules["sportsdata_engines"] = current
    try:
        with caplog.at_level("WARNING"):
            assert check_version() is not None
        assert "older than the minimum" not in caplog.text
    finally:
        del sys.modules["sportsdata_engines"]
