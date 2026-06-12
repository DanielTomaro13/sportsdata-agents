#!/usr/bin/env python
"""Build + sign the OTA data bundle (P4 M4.5) → dist/data-bundle.json.

    SPORTSDATA_DATA_PRIVKEY=<b64> python scripts/publish-data-bundle.py [--version X]

Packages the current packaged data files (market dictionary, capability labels)
and signs them with the data-feed private key (generate a keypair the same way as
the licence key: `python scripts/license.py keygen`, keep the public half baked
into builds as SPORTSDATA_DATA_PUBKEY). Upload dist/data-bundle.json as a release
asset and point SPORTSDATA_DATA_FEED_URL at it; clients run `agents update-data`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# run from a source checkout
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sportsdata_agents import __version__
from sportsdata_agents.operations.datafeed import build_bundle, sign_bundle


def main() -> int:
    parser = argparse.ArgumentParser(description="Build + sign the OTA data bundle.")
    parser.add_argument("--version", default=__version__, help="Bundle version label (default: package version).")
    parser.add_argument("--out", default="dist/data-bundle.json", help="Output path.")
    args = parser.parse_args()

    privkey = os.environ.get("SPORTSDATA_DATA_PRIVKEY")
    if not privkey:
        print("error: set SPORTSDATA_DATA_PRIVKEY (b64) — the data-feed signing key", file=sys.stderr)
        print("       generate one with: python scripts/license.py keygen", file=sys.stderr)
        return 1

    bundle = build_bundle(args.version)
    signature = sign_bundle(bundle, privkey)
    doc = {"bundle": bundle, "signature": signature}

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc), encoding="utf-8")
    files = ", ".join(bundle["files"])
    print(f"wrote {out}  (version {bundle['version']}, files: {files}, signed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
