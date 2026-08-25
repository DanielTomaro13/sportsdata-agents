"""The public site's provider browser is driven entirely by site/catalogue.json.

It is generated from the MCP specs and nothing in it is hand-maintained — but nothing
checked that it had been regenerated, so it drifted. By 2026-08-11 it advertised 29
providers and 522 tools against a real 60 and 738, and showed none of the bring-your-own
-key tier at all. It drifted again immediately after MyFantasyLeague shipped.

`gen-catalogue.py --check` existed the whole time and was wired to nothing. This is the
wiring: a site that undersells the product by a third is worse than one that says nothing,
and staleness should fail on the machine that caused it rather than in a stranger's browser.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GEN = ROOT / "scripts" / "gen-catalogue.py"
#: The generator reads the MCP provider specs, which live in a sibling repo. CI for THIS
#: repo does not check that one out, so the staleness check can only run where the
#: catalogue is actually generated and published — the operator machine. That is the
#: right place for it: this fails for whoever adds a provider, at the moment they add it.
MCP_SPECS = ROOT.parent / "sportsdata-mcp" / "src"

needs_mcp = pytest.mark.skipif(
    not (GEN.exists() and MCP_SPECS.exists()),
    reason="needs the sportsdata-mcp specs checked out beside this repo",
)


@needs_mcp
def test_the_published_catalogue_matches_the_specs():
    result = subprocess.run(
        [sys.executable, str(GEN), "--check"],
        capture_output=True, text=True, cwd=ROOT, timeout=180,
    )
    assert result.returncode == 0, (
        f"site/catalogue.json is stale — the public site would undersell the product.\n"
        f"Run: python3 scripts/gen-catalogue.py\n\n{result.stdout}{result.stderr}"
    )


def test_the_fantasy_platforms_are_all_advertised():
    """Four fantasy platforms shipped; each one has been missing from this file at some
    point, because adding a provider and republishing are separate acts."""
    import json

    catalogue = json.loads((ROOT / "site" / "catalogue.json").read_text())
    ids = {p["id"] for p in catalogue["providers"]}
    for platform in ("fpl", "espnfantasy", "myfantasyleague", "sleeper"):
        assert platform in ids, f"{platform} is not on the public site"
