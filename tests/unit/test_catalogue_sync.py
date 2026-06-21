"""The feed catalogue has ONE source (site/catalogue.json). The Worker's PROVIDER_KIND /
PROXIED_PROVIDERS are generated from it; this test fails if they drift (run
`python scripts/gen-catalogue.py` to regenerate)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def test_worker_catalogue_is_in_sync_with_the_source() -> None:
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen-catalogue.py"), "--check"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, (
        "catalogue.ts drifted from site/catalogue.json — run scripts/gen-catalogue.py\n"
        + r.stdout + r.stderr
    )


def test_every_provider_has_a_valid_kind_and_flags() -> None:
    cat = json.loads((ROOT / "site" / "catalogue.json").read_text())
    for p in cat["providers"]:
        assert p.get("kind") in {"sport", "gambling", "social"}, p["id"]
        assert isinstance(p.get("byo"), bool), p["id"]
        assert isinstance(p.get("proxied"), bool), p["id"]
